from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.utils.functional import cached_property
from django.utils.http import is_safe_url
from django.utils.translation import ugettext_lazy as _, ngettext
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from wagtail.admin import messages
from wagtail.admin.edit_handlers import Workflow
from wagtail.admin.forms.workflows import AddWorkflowToPageForm
from wagtail.admin.views.generic import CreateView, DeleteView, EditView, IndexView
from wagtail.admin.views.pages import get_valid_next_url_from_request
from wagtail.core.models import Page, Task, TaskState, WorkflowState
from wagtail.core.permissions import workflow_permission_policy, task_permission_policy


class Index(IndexView):
    permission_policy = workflow_permission_policy
    model = Workflow
    context_object_name = 'workflows'
    template_name = 'wagtailadmin/workflows/index.html'
    add_url_name = 'wagtailadmin_workflows:add'
    edit_url_name = 'wagtailadmin_workflows:edit'
    page_title = _("Workflows")
    add_item_label = _("Add a workflow")
    header_icon = 'placeholder'

    def get_queryset(self):
        queryset = super().get_queryset()
        if 'show_disabled' not in self.request.GET:
            queryset = queryset.filter(active=True)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['showing_disabled'] = 'show_disabled' in self.request.GET
        return context


class Create(CreateView):
    permission_policy = workflow_permission_policy
    model = Workflow
    page_title = _("Add workflow")
    template_name = 'wagtailadmin/workflows/create.html'
    success_message = _("Workflow '{0}' created.")
    add_url_name = 'wagtailadmin_workflows:add'
    edit_url_name = 'wagtailadmin_workflows:edit'
    index_url_name = 'wagtailadmin_workflows:index'
    header_icon = 'placeholder'
    edit_handler = None

    def get_edit_handler(self):
        if not self.edit_handler:
            self.edit_handler = self.model.get_edit_handler()
            self.edit_handler = self.edit_handler.bind_to(request=self.request)
        return self.edit_handler

    def get_form_class(self):
        return self.get_edit_handler().get_form_class()

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        self.edit_handler = self.edit_handler.bind_to(form=form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['edit_handler'] = self.edit_handler
        return context


class Edit(EditView):
    permission_policy = workflow_permission_policy
    model = Workflow
    page_title = _("Edit workflow")
    template_name = 'wagtailadmin/workflows/edit.html'
    success_message = _("Workflow '{0}' updated.")
    add_url_name = 'wagtailadmin_workflows:add'
    edit_url_name = 'wagtailadmin_workflows:edit'
    delete_url_name = 'wagtailadmin_workflows:disable'
    delete_item_label = _('Disable')
    index_url_name = 'wagtailadmin_workflows:index'
    enable_item_label = _('Enable')
    enable_url_name = 'wagtailadmin_workflows:enable'
    header_icon = 'placeholder'
    edit_handler = None
    MAX_PAGES = 5

    def get_edit_handler(self):
        if not self.edit_handler:
            self.edit_handler = self.model.get_edit_handler()
            self.edit_handler = self.edit_handler.bind_to(request=self.request, instance=self.get_object())
        return self.edit_handler

    def get_form_class(self):
        return self.get_edit_handler().get_form_class()

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        self.edit_handler = self.edit_handler.bind_to(form=form)
        return form

    def get_paginated_pages(self):
        # Get the (paginated) list of Pages to which this Workflow is assigned.
        pages = Page.objects.filter(workflowpage__workflow=self.get_object())
        pages.paginator = Paginator(pages, self.MAX_PAGES)
        page_number = int(self.request.GET.get('p', 1))
        paginated_pages = pages.paginator.page(page_number)
        return paginated_pages

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['edit_handler'] = self.edit_handler
        context['pages'] = self.get_paginated_pages()
        context['can_disable'] = (self.permission_policy is None or self.permission_policy.user_has_permission(self.request.user, 'delete')) and self.object.active
        context['can_enable'] = (self.permission_policy is None or self.permission_policy.user_has_permission(
            self.request.user, 'create')) and not self.object.active
        return context

    @property
    def get_enable_url(self):
        return reverse(self.enable_url_name, args=(self.object.pk,))


class Disable(DeleteView):
    permission_policy = workflow_permission_policy
    model = Workflow
    page_title = _("Disable workflow")
    template_name = 'wagtailadmin/workflows/confirm_disable.html'
    success_message = _("Workflow '{0}' disabled.")
    add_url_name = 'wagtailadmin_workflows:add'
    edit_url_name = 'wagtailadmin_workflows:edit'
    delete_url_name = 'wagtailadmin_workflows:disable'
    index_url_name = 'wagtailadmin_workflows:index'
    header_icon = 'placeholder'

    @property
    def get_edit_url(self):
        return reverse(self.edit_url_name, args=(self.kwargs['pk'],))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        states_in_progress = WorkflowState.objects.filter(status=WorkflowState.STATUS_IN_PROGRESS).count()
        context['warning_message'] = ngettext(
            'This workflow is in progress on %(states_in_progress)d page. Disabling this workflow will cancel moderation on this page.',
            'This workflow is in progress on %(states_in_progress)d pages. Disabling this workflow will cancel moderation on these pages.',
            states_in_progress,
        ) % {
            'states_in_progress': states_in_progress,
        }
        return context

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.object.deactivate(user=request.user)
        messages.success(request, self.get_success_message())
        return redirect(reverse(self.index_url_name))


@require_POST
def enable_workflow(request, pk):
    # Reactivate an inactive workflow
    workflow = get_object_or_404(Workflow, id=pk)

    # Check permissions
    if not workflow_permission_policy.user_has_permission(request.user, 'create'):
        raise PermissionDenied

    # Set workflow to active if inactive
    if not workflow.active:
        workflow.active = True
        workflow.save()
        messages.success(request, _("Workflow '{0}' enabled.").format(workflow.name))

    # Redirect
    redirect_to = request.POST.get('next', None)
    if redirect_to and is_safe_url(url=redirect_to, allowed_hosts={request.get_host()}):
        return redirect(redirect_to)
    else:
        return redirect('wagtailadmin_workflows:edit', workflow.id)


class Delete(DeleteView):
    permission_policy = workflow_permission_policy
    model = Workflow
    page_title = _("Delete workflow")
    template_name = 'wagtailadmin/workflows/confirm_delete.html'
    success_message = _("Workflow '{0}' deleted.")
    add_url_name = 'wagtailadmin_workflows:add'
    edit_url_name = 'wagtailadmin_workflows:edit'
    delete_url_name = 'wagtailadmin_workflows:delete'
    index_url_name = 'wagtailadmin_workflows:index'
    header_icon = 'placeholder'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['workflow_states_in_progress'] = WorkflowState.objects.filter(status=WorkflowState.STATUS_IN_PROGRESS).count()
        return context



@require_POST
def remove_workflow(request, page_pk, workflow_pk=None):
    # Remove a workflow from a page (specifically a single workflow if workflow_pk is set)
    # Get the page
    page = get_object_or_404(Page, id=page_pk)

    # Check permissions
    if not workflow_permission_policy.user_has_permission(request.user, 'remove_from_page'):
        raise PermissionDenied

    if hasattr(page, 'workflowpage'):
        # If workflow_pk is set, this will only remove the workflow if it its pk matches - this prevents accidental
        # removal of the wrong workflow via a workflow edit page if the page listing is out of date
        if not workflow_pk or workflow_pk == page.workflowpage.workflow.pk:
            page.workflowpage.delete()
            messages.success(request, _("Workflow removed from Page '{0}'.").format(page.get_admin_display_title()))

    # Redirect
    redirect_to = request.POST.get('next', None)
    if redirect_to and is_safe_url(url=redirect_to, allowed_hosts={request.get_host()}):
        return redirect(redirect_to)
    else:
        return redirect('wagtailadmin_explore', page.id)


def add_to_page(request, workflow_pk):
    # Assign a workflow to a Page, including a confirmation step if the Page has a different Workflow assigned already.

    if not workflow_permission_policy.user_has_permission(request.user, 'add_to_page'):
        raise PermissionDenied

    workflow = get_object_or_404(Workflow, pk=workflow_pk)
    form_class = AddWorkflowToPageForm

    next_url = get_valid_next_url_from_request(request)
    if request.method == 'POST':
        form = form_class(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, _("Workflow '{0}' added to Page '{1}'.").format(workflow, form.cleaned_data['page']))
            form = form_class(initial={'workflow': workflow.pk, 'overwrite_existing': False})

    else:
        form = form_class(initial={'workflow': workflow.pk, 'overwrite_existing': False})

    confirm = form.has_error('page', 'needs_confirmation')

    return render(request, 'wagtailadmin/workflows/add_to_page.html', {
        'workflow': workflow,
        'form': form,
        'icon': 'placeholder',
        'title': _("Workflows"),
        'next': next_url,
        'confirm': confirm
    })


class TaskIndex(IndexView):
    permission_policy = task_permission_policy
    model = Task
    context_object_name = 'tasks'
    template_name = 'wagtailadmin/workflows/task_index.html'
    add_url_name = 'wagtailadmin_workflows:select_task_type'
    edit_url_name = 'wagtailadmin_workflows:edit_task'
    page_title = _("Tasks")
    add_item_label = _("Add a task")
    header_icon = 'placeholder'

    def get_queryset(self):
        queryset = super().get_queryset()
        if 'show_disabled' not in self.request.GET:
            queryset = queryset.filter(active=True)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['showing_disabled'] = 'show_disabled' in self.request.GET
        return context


def select_task_type(request):
    if not task_permission_policy.user_has_permission(request.user, 'add'):
        raise PermissionDenied

    task_types = [
        (model.get_verbose_name(), model._meta.app_label, model._meta.model_name)
        for model in Task.__subclasses__()
    ]
    # sort by lower-cased version of verbose name
    task_types.sort(key=lambda task_type: task_type[0].lower())

    if len(task_types) == 1:
        # Only one task type is available - redirect straight to the create form rather than
        # making the user choose
        verbose_name, app_label, model_name = task_types[0]
        return redirect('wagtailadmin_workflows:add_task', app_label, model_name)

    return render(request, 'wagtailadmin/workflows/select_task_type.html', {
        'task_types': task_types,
        'icon': 'placeholder',
        'title': _("Workflows"),
    })


class CreateTask(CreateView):
    permission_policy = task_permission_policy
    model = None
    page_title = _("Add task")
    template_name = 'wagtailadmin/workflows/create_task.html'
    success_message = _("Task '{0}' created.")
    add_url_name = 'wagtailadmin_workflows:add_task'
    edit_url_name = 'wagtailadmin_workflows:edit_task'
    index_url_name = 'wagtailadmin_workflows:task_index'
    header_icon = 'placeholder'
    edit_handler = None

    @cached_property
    def model(self):
        try:
            content_type = ContentType.objects.get_by_natural_key(self.kwargs['app_label'], self.kwargs['model_name'])
        except (ContentType.DoesNotExist, AttributeError):
            raise Http404

        # Get class
        model = content_type.model_class()

        # Make sure the class is a descendant of Task
        if not issubclass(model, Task):
            raise Http404

        return model

    def get_edit_handler(self):
        if not self.edit_handler:
            self.edit_handler = self.model.get_edit_handler()
            self.edit_handler = self.edit_handler.bind_to(request=self.request)
        return self.edit_handler

    def get_form_class(self):
        return self.get_edit_handler().get_form_class()

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        self.edit_handler = self.edit_handler.bind_to(form=form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['edit_handler'] = self.edit_handler
        return context

    def get_add_url(self):
        return reverse(self.add_url_name, kwargs={'app_label': self.kwargs.get('app_label'), 'model_name': self.kwargs.get('model_name')})


class EditTask(EditView):
    permission_policy = task_permission_policy
    model = None
    page_title = _("Edit task")
    template_name = 'wagtailadmin/workflows/edit_task.html'
    success_message = _("Task '{0}' updated.")
    add_url_name = 'wagtailadmin_workflows:select_task_type'
    edit_url_name = 'wagtailadmin_workflows:edit_task'
    delete_url_name = 'wagtailadmin_workflows:disable_task'
    index_url_name = 'wagtailadmin_workflows:task_index'
    delete_item_label = _('Disable')
    enable_item_label = _('Enable')
    enable_url_name = 'wagtailadmin_workflows:enable_task'
    header_icon = 'placeholder'
    edit_handler = None

    @cached_property
    def model(self):
        return type(self.get_object())

    def get_queryset(self):
        if self.queryset is None:
            return Task.objects.all()

    def get_object(self, queryset=None):
        return super().get_object().specific

    def get_edit_handler(self):
        if not self.edit_handler:
            self.edit_handler = self.model.get_edit_handler()
            self.edit_handler = self.edit_handler.bind_to(request=self.request, instance=self.get_object())
        return self.edit_handler

    def get_form_class(self):
        return self.get_edit_handler().get_form_class()

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        self.edit_handler = self.edit_handler.bind_to(form=form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['edit_handler'] = self.edit_handler
        context['can_disable'] = (self.permission_policy is None or self.permission_policy.user_has_permission(self.request.user, 'delete')) and self.object.active
        context['can_enable'] = (self.permission_policy is None or self.permission_policy.user_has_permission(self.request.user, 'create')) and not self.object.active

        # TODO: add warning msg when there are pages currently on this task in a workflow, add interaction like resetting task state when saved
        return context

    @property
    def get_enable_url(self):
        return reverse(self.enable_url_name, args=(self.object.pk,))


class DisableTask(DeleteView):
    permission_policy = task_permission_policy
    model = Task
    page_title = _("Disable task")
    template_name = 'wagtailadmin/workflows/confirm_disable_task.html'
    success_message = _("Task '{0}' disabled.")
    add_url_name = 'wagtailadmin_workflows:add_task'
    edit_url_name = 'wagtailadmin_workflows:edit_task'
    delete_url_name = 'wagtailadmin_workflows:disable_task'
    index_url_name = 'wagtailadmin_workflows:task_index'
    header_icon = 'placeholder'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        states_in_progress = TaskState.objects.filter(status=TaskState.STATUS_IN_PROGRESS).count()
        context['warning_message'] = ngettext(
            'This task is in progress on %(states_in_progress)d page. Disabling this task will cause it to be skipped in the moderation workflow.',
            'This task is in progress on %(states_in_progress)d pages. Disabling this task will cause it to be skipped in the moderation workflow.',
            states_in_progress,
        ) % {
            'states_in_progress': states_in_progress,
        }
        return context

    @property
    def get_edit_url(self):
        return reverse(self.edit_url_name, args=(self.kwargs['pk'],))

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.object.deactivate(user=request.user)
        messages.success(request, self.get_success_message())
        return redirect(reverse(self.index_url_name))


@require_POST
def enable_task(request, pk):
    # Reactivate an inactive task
    task = get_object_or_404(Task, id=pk)

    # Check permissions
    if not task_permission_policy.user_has_permission(request.user, 'create'):
        raise PermissionDenied

    # Set workflow to active if inactive
    if not task.active:
        task.active = True
        task.save()
        messages.success(request, _("Task '{0}' enabled.").format(task.name))

    # Redirect
    redirect_to = request.POST.get('next', None)
    if redirect_to and is_safe_url(url=redirect_to, allowed_hosts={request.get_host()}):
        return redirect(redirect_to)
    else:
        return redirect('wagtailadmin_workflows:edit_task', task.id)