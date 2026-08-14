"""Workflows package -- reusable, source-agnostic pipelines.

Register the built-in workflows here so that `workflow_registry.route_event`
can route incoming events to the right pipeline automatically.
"""

from .base import (
    BaseWorkflow,
    WorkflowResult,
    WorkflowRegistry,
    WorkflowAborted,
    workflow_registry,
)
from .assignment import HandleAssignmentWorkflow
from .email import HandleNewEmailWorkflow
from .document import ProcessDocumentWorkflow
from .meeting import ScheduleEventWorkflow
from .briefing import DailyBriefingWorkflow


def register_all():
    workflow_registry.register(HandleAssignmentWorkflow())
    workflow_registry.register(HandleNewEmailWorkflow())
    workflow_registry.register(ProcessDocumentWorkflow())
    workflow_registry.register(ScheduleEventWorkflow())
    workflow_registry.register(DailyBriefingWorkflow())
    return workflow_registry


__all__ = [
    "BaseWorkflow", "WorkflowResult", "WorkflowRegistry", "WorkflowAborted",
    "workflow_registry", "register_all",
    "HandleAssignmentWorkflow", "HandleNewEmailWorkflow",
    "ProcessDocumentWorkflow", "ScheduleEventWorkflow", "DailyBriefingWorkflow",
]