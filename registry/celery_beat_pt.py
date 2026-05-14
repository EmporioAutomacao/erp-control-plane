"""Tradução do django-celery-beat para português."""
from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Case, Value, When
from django import forms
from kombu.utils.json import loads

from django_celery_beat.admin import TaskChoiceField
from django_celery_beat.models import (
    ClockedSchedule, CrontabSchedule, IntervalSchedule,
    PeriodicTask, PeriodicTasks, SolarSchedule,
)
from django_celery_beat.utils import is_database_scheduler
from celery import current_app

# --- Tradução dos verbose_names ---
PeriodicTask._meta.verbose_name = 'Tarefa Periódica'
PeriodicTask._meta.verbose_name_plural = 'Tarefas Periódicas'
CrontabSchedule._meta.verbose_name = 'Agendamento Cron'
CrontabSchedule._meta.verbose_name_plural = 'Agendamentos Cron'
IntervalSchedule._meta.verbose_name = 'Agendamento por Intervalo'
IntervalSchedule._meta.verbose_name_plural = 'Agendamentos por Intervalo'
ClockedSchedule._meta.verbose_name = 'Agendamento por Horário'
ClockedSchedule._meta.verbose_name_plural = 'Agendamentos por Horário'
SolarSchedule._meta.verbose_name = 'Agendamento Solar'
SolarSchedule._meta.verbose_name_plural = 'Agendamentos Solares'


class FormTarefaPeriodica(forms.ModelForm):
    regtask = TaskChoiceField(label='Tarefa (registrada)', required=False)
    task = forms.CharField(label='Tarefa (personalizada)', required=False, max_length=200)

    class Meta:
        model = PeriodicTask
        exclude = ()

    def clean(self):
        data = super().clean()
        regtask = data.get('regtask')
        if regtask:
            data['task'] = regtask
        if not data['task']:
            exc = forms.ValidationError('Informe o nome da tarefa.')
            self._errors['task'] = self.error_class(exc.messages)
            raise exc
        if data.get('expire_seconds') is not None and data.get('expires'):
            raise forms.ValidationError(
                'Defina apenas um: expiração em data ou em segundos.'
            )
        return data

    def _clean_json(self, field):
        value = self.cleaned_data[field]
        try:
            loads(value)
        except ValueError as exc:
            raise forms.ValidationError(f'JSON inválido: {exc}')
        return value

    def clean_args(self):
        return self._clean_json('args')

    def clean_kwargs(self):
        return self._clean_json('kwargs')


class TarefaPeriodicaInline(admin.TabularInline):
    model = PeriodicTask
    fields = ('name', 'task', 'args', 'kwargs')
    readonly_fields = fields
    can_delete = False
    extra = 0
    show_change_link = True
    verbose_name = 'Tarefa Periódica que usa este agendamento'
    verbose_name_plural = 'Tarefas Periódicas que usam este agendamento'

    def has_add_permission(self, request, obj):
        return False


class AgendamentoBaseAdmin(admin.ModelAdmin):
    inlines = [TarefaPeriodicaInline]


# --- Desregistrar admins originais ---
admin.site.unregister(PeriodicTask)
admin.site.unregister(CrontabSchedule)
admin.site.unregister(IntervalSchedule)
admin.site.unregister(ClockedSchedule)
admin.site.unregister(SolarSchedule)


@admin.register(PeriodicTask)
class TarefaPeriodicaAdmin(admin.ModelAdmin):
    form = FormTarefaPeriodica
    model = PeriodicTask
    celery_app = current_app
    date_hierarchy = 'start_time'
    list_display = ('name', 'enabled', 'scheduler', 'interval', 'start_time', 'last_run_at', 'one_off')
    list_filter = ['enabled', 'one_off', 'task', 'start_time', 'last_run_at']
    actions = ('habilitar_tarefas', 'desabilitar_tarefas', 'alternar_tarefas', 'executar_tarefas')
    search_fields = ('name', 'task')
    fieldsets = (
        (None, {
            'fields': ('name', 'regtask', 'task', 'enabled', 'description'),
            'classes': ('extrapretty', 'wide'),
        }),
        ('Agendamento', {
            'fields': ('interval', 'crontab', 'crontab_translation', 'solar',
                       'clocked', 'start_time', 'last_run_at', 'one_off'),
            'classes': ('extrapretty', 'wide'),
        }),
        ('Argumentos', {
            'fields': ('args', 'kwargs'),
            'classes': ('extrapretty', 'wide', 'collapse', 'in'),
        }),
        ('Opções de Execução', {
            'fields': ('expires', 'expire_seconds', 'queue', 'exchange',
                       'routing_key', 'priority', 'headers'),
            'classes': ('extrapretty', 'wide', 'collapse', 'in'),
        }),
    )
    readonly_fields = ('last_run_at', 'crontab_translation')
    change_form_template = 'admin/djcelery/change_periodictask_form.html'

    def crontab_translation(self, obj):
        return obj.crontab.human_readable
    crontab_translation.short_description = 'Expressão legível'

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        extra_context['readable_crontabs'] = {
            c.id: c.human_readable for c in CrontabSchedule.objects.all()
        }
        return super().changeform_view(request, object_id, extra_context=extra_context)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        scheduler = getattr(settings, 'CELERY_BEAT_SCHEDULER', None)
        extra_context['wrong_scheduler'] = not is_database_scheduler(scheduler)
        return super().changelist_view(request, extra_context)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('interval', 'crontab', 'solar', 'clocked')

    @admin.action(description='Habilitar tarefas selecionadas')
    def habilitar_tarefas(self, request, queryset):
        atualizadas = queryset.update(enabled=True)
        PeriodicTasks.update_changed()
        self.message_user(request, f'{atualizadas} tarefa(s) habilitada(s) com sucesso.')

    @admin.action(description='Desabilitar tarefas selecionadas')
    def desabilitar_tarefas(self, request, queryset):
        atualizadas = queryset.update(enabled=False, last_run_at=None)
        PeriodicTasks.update_changed()
        self.message_user(request, f'{atualizadas} tarefa(s) desabilitada(s) com sucesso.')

    @admin.action(description='Alternar atividade das tarefas selecionadas')
    def alternar_tarefas(self, request, queryset):
        atualizadas = queryset.update(enabled=Case(
            When(enabled=True, then=Value(False)),
            default=Value(True),
        ))
        PeriodicTasks.update_changed()
        self.message_user(request, f'{atualizadas} tarefa(s) alternada(s) com sucesso.')

    @admin.action(description='Executar tarefas selecionadas')
    def executar_tarefas(self, request, queryset):
        self.celery_app.loader.import_default_modules()
        tasks = [
            (self.celery_app.tasks.get(t.task), loads(t.args), loads(t.kwargs), t.queue, t.name)
            for t in queryset
        ]
        for i, (task, *_) in enumerate(tasks):
            if task is None:
                self.message_user(
                    request,
                    f'Tarefa "{queryset[i].task}" não encontrada.',
                    level=messages.ERROR,
                )
                return
        task_ids = [
            task.apply_async(args=args, kwargs=kwargs, queue=queue,
                             headers={'periodic_task_name': nome})
            if queue else
            task.apply_async(args=args, kwargs=kwargs,
                             headers={'periodic_task_name': nome})
            for task, args, kwargs, queue, nome in tasks
        ]
        self.message_user(request, f'{len(task_ids)} tarefa(s) executada(s) com sucesso.')


@admin.register(ClockedSchedule)
class AgendamentoRelogioAdmin(AgendamentoBaseAdmin):
    fields = ('clocked_time',)
    list_display = ('clocked_time',)


@admin.register(CrontabSchedule)
class AgendamentoCronAdmin(AgendamentoBaseAdmin):
    list_display = ('__str__', 'human_readable')
    fields = ('human_readable', 'minute', 'hour', 'day_of_month', 'month_of_year', 'day_of_week', 'timezone')
    readonly_fields = ('human_readable',)


@admin.register(SolarSchedule)
class AgendamentoSolarAdmin(AgendamentoBaseAdmin):
    pass


@admin.register(IntervalSchedule)
class AgendamentoIntervaloAdmin(AgendamentoBaseAdmin):
    pass
