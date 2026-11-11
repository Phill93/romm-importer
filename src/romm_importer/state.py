"""Validated job state transitions."""

from .models import JobStatus

ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.CREATED: {
        JobStatus.INVENTORYING,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    },
    JobStatus.INVENTORYING: {
        JobStatus.ANALYZING,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    },
    JobStatus.ANALYZING: {
        JobStatus.AWAITING_APPROVAL,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    },
    JobStatus.CONVERTING: {
        JobStatus.VALIDATING,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    },
    JobStatus.VALIDATING: {
        JobStatus.AWAITING_APPROVAL,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    },
    JobStatus.AWAITING_APPROVAL: {JobStatus.PUBLISHING, JobStatus.CANCELLED},
    JobStatus.PUBLISHING: {JobStatus.SCANNING, JobStatus.FAILED, JobStatus.INTERRUPTED},
    JobStatus.SCANNING: {JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
    JobStatus.INTERRUPTED: {JobStatus.PUBLISHING, JobStatus.SCANNING},
}


def transition_allowed(current: JobStatus, target: JobStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]
