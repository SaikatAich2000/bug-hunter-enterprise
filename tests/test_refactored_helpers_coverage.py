"""Tests for refactored helper functions - focus on agile.py helpers for coverage.

Covers:
  - app.routes.agile: _validate_story_ready_requirements, _apply_task_flag_updates
"""
from datetime import date

import pytest

from app.auth import hash_password
from app.models import ROLE_ADMIN, AcceptanceCriterion, Bug, Project, User
from app.routes.agile import _apply_task_flag_updates, _validate_story_ready_requirements
from app.schemas import WorkItemTaskFlagsIn
from tests.conftest import default_org_id


@pytest.fixture
def test_project_and_user(db_session):
    """Create a project and user for tests."""
    project = Project(org_id=default_org_id(), name="TestProj", color="#000", agile_enabled=True)
    db_session.add(project)
    db_session.flush()
    
    user = User(org_id=default_org_id(), email="test@ex.com", name="Test", role=ROLE_ADMIN, password_hash=hash_password("p"))
    db_session.add(user)
    db_session.flush()
    
    return project, user


@pytest.fixture
def task_project(db_session):
    """Create the required project for standalone task flag tests."""
    project = Project(org_id=default_org_id(), name="TaskProj", color="#111", agile_enabled=True)
    db_session.add(project)
    db_session.flush()
    return project


class TestValidateStoryReadyRequirements:
    """Tests for _validate_story_ready_requirements helper."""

    def test_returns_false_when_description_missing(self, db_session, test_project_and_user):
        """Returns False if Story has empty description."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        # Manually set description to empty
        story.description = ""
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_priority(self, db_session, test_project_and_user):
        """Returns False if Story has no priority."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="", story_points=5, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_story_points(self, db_session, test_project_and_user):
        """Returns False if Story has no story points."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=None, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_owner_or_assignees(self, db_session, test_project_and_user):
        """Returns False if Story has neither owner nor assignees."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=None,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_start_date(self, db_session, test_project_and_user):
        """Returns False if Story has no start date."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=None, due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_due_date(self, db_session, test_project_and_user):
        """Returns False if Story has no due date."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=None
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_acceptance_criteria(self, db_session, test_project_and_user):
        """Returns False if Story has no acceptance criteria."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        # No acceptance criterion added
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_true_when_all_requirements_met(self, db_session, test_project_and_user):
        """Returns True if Story has all mandatory fields."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert _validate_story_ready_requirements(db_session, story)


class TestApplyTaskFlagUpdates:
    """Tests for _apply_task_flag_updates helper - extensive edge cases."""

    def test_apply_mandatory_flag_true(self, db_session, task_project):
        """Sets mandatory flag to True."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   mandatory=False, blocked=False)
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, mandatory=True)
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is True
        assert blocked_changed is False

    def test_apply_mandatory_flag_false(self, db_session, task_project):
        """Sets mandatory flag to False."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   mandatory=True, blocked=False)
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, mandatory=False)
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is False
        assert blocked_changed is False

    def test_apply_blocked_flag_true(self, db_session, task_project):
        """Sets blocked flag to True with reason."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   blocked=False, blocked_reason="")
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, blocked=True, blocked_reason="Waiting for approval")
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.blocked is True
        assert task.blocked_reason == "Waiting for approval"
        assert blocked_changed is True

    def test_apply_blocked_flag_false_clears_reason(self, db_session, task_project):
        """Sets blocked flag to False and clears reason."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   blocked=True, blocked_reason="Dependency")
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, blocked=False)
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.blocked is False
        assert task.blocked_reason == ""
        assert blocked_changed is True

    def test_apply_blocked_reason_update(self, db_session, task_project):
        """Updates reason without changing blocked status."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   blocked=True, blocked_reason="Old")
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, blocked_reason="New reason")
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.blocked_reason == "New reason"
        assert task.blocked is True
        assert blocked_changed is False  # No change to blocked flag itself

    def test_no_change_when_no_fields_provided(self, db_session, task_project):
        """Returns False when no fields are being updated."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   mandatory=True, blocked=False)
        db_session.add(task)
        db_session.flush()
        original_mandatory = task.mandatory
        
        payload = WorkItemTaskFlagsIn(version=1)  # All fields None
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is original_mandatory
        assert task.blocked is False
        assert blocked_changed is False

    def test_multiple_flags_at_once(self, db_session, task_project):
        """Updates multiple flags simultaneously."""
        task = Bug(
            item_type="Sub-task", title="T", description="D", project_id=task_project.id,
            mandatory=False, blocked=False, blocked_reason=""
        )
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(
            version=1, mandatory=True, blocked=True, blocked_reason="Complex dep"
        )
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is True
        assert task.blocked is True
        assert task.blocked_reason == "Complex dep"
        assert blocked_changed is True

    def test_unblock_and_set_mandatory(self, db_session, task_project):
        """Unblock an item and set mandatory flag in one call."""
        task = Bug(
            item_type="Sub-task", title="T", description="D", project_id=task_project.id,
            mandatory=False, blocked=True, blocked_reason="Old issue"
        )
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, mandatory=True, blocked=False)
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is True
        assert task.blocked is False
        assert task.blocked_reason == ""
        assert blocked_changed is True

    def test_update_reason_only(self, db_session, task_project):
        """Update only the blocked reason without touching flags."""
        task = Bug(
            item_type="Sub-task", title="T", description="D", project_id=task_project.id,
            mandatory=True, blocked=True, blocked_reason="Initial"
        )
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, blocked_reason="Updated reason")
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is True  # Unchanged
        assert task.blocked is True  # Unchanged
        assert task.blocked_reason == "Updated reason"
        assert blocked_changed is False
