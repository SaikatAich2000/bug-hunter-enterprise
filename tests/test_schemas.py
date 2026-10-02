"""Pure-unit tests for app/schemas.py validators and the HTML sanitizer.
app.schemas is imported lazily via _S() because conftest re-imports app.* between tests."""
import pytest
from pydantic import ValidationError


def _S():
    """Return app.schemas for the current import generation."""
    import app.schemas as schemas
    return schemas


# HTML sanitizer (_HTMLAllowlistSanitizer / sanitize_html)
def test_cov_sanitize_html_none_returns_empty():
    # None input returns an empty string.
    s = _S()
    assert s.sanitize_html(None) == ""


def test_cov_sanitize_html_keeps_allowed_strips_disallowed():
    s = _S()
    # <b> is kept; <script> and its body are dropped entirely. Re-emitting
    # the body even escaped is a parser-differential hazard, so we strip it.
    # Surrounding text ("x") must survive.
    out = s.sanitize_html("<b>hi</b><script>alert(1)</script>x")
    assert "<b>hi</b>" in out
    assert "<script>" not in out
    assert "alert(1)" not in out
    assert out.endswith(">x")
    assert out == "<b>hi</b>x"


def test_cov_sanitize_html_idempotent():
    s = _S()
    once = s.sanitize_html("<b>hi</b><script>bad</script>")
    twice = s.sanitize_html(once)
    assert once == twice


def test_cov_sanitize_url_empty_returns_none():
    # An empty href is dropped; the <a> tag is still emitted with the forced rel.
    s = _S()
    out = s.sanitize_html('<a href="">link</a>')
    assert 'href=' not in out
    assert 'rel="noopener nofollow"' in out


def test_cov_sanitize_url_data_image_ok_and_too_big():
    # A small data:image is kept; one over the 14 MB limit has its src stripped.
    s = _S()
    small = '<img src="data:image/png;base64,AAAA" alt="x">'
    out = s.sanitize_html(small)
    assert 'src="data:image/png;base64,AAAA"' in out
    big_payload = "A" * (14 * 1024 * 1024 + 10)
    big = f'<img src="data:image/png;base64,{big_payload}" alt="x">'
    out_big = s.sanitize_html(big)
    assert "data:image" not in out_big
    assert "<img" in out_big  # tag kept, just the src stripped


def test_cov_sanitize_url_allowed_scheme_kept():
    # https: is an allowed scheme and is kept unchanged.
    s = _S()
    out = s.sanitize_html('<a href="https://example.com">ex</a>')
    assert 'href="https://example.com"' in out


def test_cov_sanitize_url_disallowed_scheme_dropped():
    # javascript: is not an allowed scheme, so the href is stripped.
    s = _S()
    out = s.sanitize_html('<a href="javascript:alert(1)">x</a>')
    assert "javascript:" not in out


def test_cov_sanitize_attr_disallowed_dropped():
    # onclick is not in <a>'s allowed attribute set, so it is dropped.
    s = _S()
    out = s.sanitize_html('<a href="https://e.com" onclick="evil()">y</a>')
    assert "onclick" not in out
    assert 'href="https://e.com"' in out


def test_cov_sanitize_attr_none_value_skipped():
    # <a href> — href is in the allowlist but has no value, so it is skipped.
    s = _S()
    out = s.sanitize_html("<a href>link</a>")
    assert "href=" not in out
    assert "<a" in out  # tag kept, forced rel still added
    assert 'rel="noopener nofollow"' in out


def test_cov_sanitize_attr_value_html_escaped():
    # Attribute values are HTML-escaped on output.
    s = _S()
    out = s.sanitize_html('<a href="https://e.com" title=\'a"b<c&d\'>x</a>')
    assert "&quot;" in out
    assert "&lt;" in out
    assert "&amp;" in out


def test_cov_sanitize_a_tag_forces_rel_over_user_value():
    # Any user-supplied rel is dropped and replaced with "noopener nofollow" to
    # prevent rel="" or rel="opener" from stripping reverse-tabnabbing protection.
    s = _S()
    out = s.sanitize_html('<a href="https://e.com" rel="author">x</a>')
    assert out.count("rel=") == 1
    assert 'rel="noopener nofollow"' in out
    assert "author" not in out
    # A missing rel also gets the forced value.
    out2 = s.sanitize_html('<a href="https://e.com">y</a>')
    assert 'rel="noopener nofollow"' in out2


def test_cov_sanitize_a_tag_forces_rel_when_missing():
    s = _S()
    out = s.sanitize_html('<a href="https://e.com">x</a>')
    assert 'rel="noopener nofollow"' in out


def test_cov_sanitize_void_elements_no_closing_tag():
    # Closing tags for void elements (br, img) are silently discarded.
    s = _S()
    out = s.sanitize_html("<br></br>")
    assert out == "<br>"
    out_img = s.sanitize_html("<img src=\"https://e.com/a.png\"></img>")
    assert out_img.count("<img") == 1
    assert "</img>" not in out_img


def test_cov_sanitize_endtag_disallowed_dropped():
    # handle_endtag early-returns for non-allowed tags such as </script>.
    s = _S()
    out = s.sanitize_html("text</script>")
    assert "</script>" not in out
    assert out.startswith("text")


def test_cov_sanitize_startendtag_self_closing():
    # <br/> goes through handle_startendtag and is normalized to <br>.
    s = _S()
    out = s.sanitize_html("a<br/>b")
    assert out == "a<br>b"


def test_cov_sanitize_entityref_and_charref_preserved():
    # Named entity refs (&amp;) and numeric char refs (&#169;) pass through unchanged.
    s = _S()
    out = s.sanitize_html("A&amp;B &#169; C")
    assert "&amp;" in out
    assert "&#169;" in out


def test_cov_sanitize_entityref_and_charref_dropped_inside_rcdata():
    # Inside an RCDATA-drop tag, entity/char refs must be dropped along with
    # the rest of the text content, not just plain text (handle_data).
    s = _S()
    assert s.sanitize_html("<xmp>&amp;x</xmp>") == ""
    assert s.sanitize_html("<title>&#65;</title>") == ""


# normalize_choice (public helper)
def test_cov_normalize_choice_canonicalizes():
    s = _S()
    assert s.normalize_choice("bug", s.ALLOWED_ITEM_TYPES, "item_type") == "Bug"
    assert s.normalize_choice("  HIGH ", s.ALLOWED_PRIORITIES, "priority") == "High"


def test_cov_normalize_choice_invalid_value_raises():
    s = _S()
    with pytest.raises(ValueError):
        s.normalize_choice("nope", s.ALLOWED_ITEM_TYPES, "item_type")


def test_cov_normalize_choice_non_string_raises():
    # Non-string input raises ValueError.
    s = _S()
    with pytest.raises(ValueError):
        s.normalize_choice(123, s.ALLOWED_ITEM_TYPES, "item_type")


# _strip_and_check_min_length
def test_cov_strip_and_check_min_length_ok():
    s = _S()
    assert s._strip_and_check_min_length("  abc  ", 2, "Name") == "abc"


def test_cov_strip_and_check_min_length_non_string_raises():
    # Non-string input raises ValueError.
    s = _S()
    with pytest.raises(ValueError):
        s._strip_and_check_min_length(123, 2, "Name")


def test_cov_strip_and_check_min_length_too_short_raises():
    # Below min_len (when min_len != 1) produces an "at least N characters" error.
    s = _S()
    with pytest.raises(ValueError):
        s._strip_and_check_min_length("a", 3, "Title")


def test_cov_strip_and_check_min_length_empty_when_min_one():
    # When min_len is 1, an empty/whitespace string produces a "cannot be empty" error.
    s = _S()
    with pytest.raises(ValueError) as exc:
        s._strip_and_check_min_length("   ", 1, "Field")
    assert "cannot be empty" in str(exc.value)


# _normalize_role
def test_cov_normalize_role_ok():
    s = _S()
    assert s._normalize_role("  ADMIN ") == "admin"


def test_cov_normalize_role_non_string_raises():
    # Non-string input raises ValueError.
    s = _S()
    with pytest.raises(ValueError):
        s._normalize_role(5)


def test_cov_normalize_role_invalid_raises():
    s = _S()
    with pytest.raises(ValueError):
        s._normalize_role("superuser")


# _validate_email
def test_cov_validate_email_ok_lowercased():
    s = _S()
    assert s._validate_email("  USER@Example.COM ") == "user@example.com"


def test_cov_validate_email_invalid_raises():
    s = _S()
    with pytest.raises(ValueError):
        s._validate_email("not-an-email")


# _check_password_strength (including the legacy exception)
def test_cov_password_legacy_default_exception_always_passes(monkeypatch):
    # The legacy value bypasses normal strength checks regardless of the configured minimum.
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_MIN_LENGTH", 24)
    s = _S()
    assert s._check_password_strength("legacy-default") == "legacy-default"
    assert s._check_password_strength("LEGACY-DEFAULT") == "LEGACY-DEFAULT"


def test_cov_password_valid_default():
    s = _S()
    assert s._check_password_strength("abcd1234") == "abcd1234"


def test_cov_password_non_string_raises():
    # Non-string input raises ValueError.
    s = _S()
    with pytest.raises(ValueError):
        s._check_password_strength(12345678)


def test_cov_password_too_long_raises():
    # A password over 200 characters trips the length guard.
    s = _S()
    with pytest.raises(ValueError):
        s._check_password_strength("a1" * 200)


def test_cov_password_too_short_raises():
    s = _S()
    with pytest.raises(ValueError):
        s._check_password_strength("ab12")


def test_cov_password_missing_digit_raises():
    s = _S()
    with pytest.raises(ValueError):
        s._check_password_strength("abcdefgh")


def test_cov_password_common_list_raises():
    s = _S()
    with pytest.raises(ValueError):
        s._check_password_strength("password123")


def test_cov_password_complexity_off_allows_letters_only(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_REQUIRE_COMPLEXITY", False)
    s = _S()
    assert s._check_password_strength("abcdefgh") == "abcdefgh"


# UserIn
def test_cov_userin_valid():
    s = _S()
    u = s.UserIn(name="  Alice ", email="A@B.com", role="ADMIN",
                 password="abcd1234")
    assert u.name == "Alice"
    assert u.email == "a@b.com"
    assert u.role == "admin"


def test_cov_userin_short_name_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserIn(name="a", email="a@b.com", password="abcd1234")


def test_cov_userin_bad_email_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserIn(name="Alice", email="bad", password="abcd1234")


def test_cov_userin_bad_role_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserIn(name="Alice", email="a@b.com", role="root", password="abcd1234")


def test_cov_userin_weak_password_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserIn(name="Alice", email="a@b.com", password="short")


def test_cov_userin_legacy_default_password_passes():
    # The legacy value must pass the model-level check as well.
    s = _S()
    u = s.UserIn(name="Alice", email="a@b.com", password="legacy-default")
    assert u.password == "legacy-default"


# --- UserUpdate  (Optional fields: None -> early-return branches) ---
def test_cov_userupdate_all_none_passes():
    # Pydantic only runs a field validator when a value is explicitly supplied,
    # so we pass None explicitly to exercise each optional validator's early-return.
    s = _S()
    u = s.UserUpdate(name=None, email=None, role=None, password=None,
                     is_active=None, project_ids=None)
    assert u.name is None
    assert u.email is None
    assert u.role is None
    assert u.password is None
    assert u.is_active is None
    assert u.project_ids is None


def test_cov_userupdate_project_ids_dedup():
    s = _S()
    u = s.UserUpdate(project_ids=[3, 1, 3, 2, 1])
    assert u.project_ids == [3, 1, 2]


def test_cov_userupdate_values_validated():
    s = _S()
    u = s.UserUpdate(name="  Bob ", email="B@C.com", role="Manager",
                     password="abcd1234")
    assert u.name == "Bob"
    assert u.email == "b@c.com"
    assert u.role == "manager"


def test_cov_userupdate_bad_name_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserUpdate(name="x")


def test_cov_userupdate_bad_email_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserUpdate(email="nope")


def test_cov_userupdate_bad_role_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserUpdate(role="root")


def test_cov_userupdate_bad_password_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserUpdate(password="short")


# --- Auth schemas ---
def test_cov_loginin_email_validated():
    s = _S()
    assert s.LoginIn(email="A@B.com", password="x").email == "a@b.com"
    with pytest.raises(ValidationError):
        s.LoginIn(email="bad", password="x")


def test_cov_changepassword_validates_new():
    s = _S()
    ok = s.ChangePasswordIn(current_password="anything", new_password="abcd1234")
    assert ok.new_password == "abcd1234"
    with pytest.raises(ValidationError):
        s.ChangePasswordIn(current_password="anything", new_password="short")
    # Field(min_length=1) catches an empty current_password before the validator.
    with pytest.raises(ValidationError):
        s.ChangePasswordIn(current_password="", new_password="abcd1234")


def test_cov_forgotpassword_email_validated():
    s = _S()
    assert s.ForgotPasswordIn(email="A@B.com").email == "a@b.com"
    with pytest.raises(ValidationError):
        s.ForgotPasswordIn(email="bad")


def test_cov_resetpassword_validates_new():
    s = _S()
    assert s.ResetPasswordIn(token="t", new_password="abcd1234").new_password == "abcd1234"
    with pytest.raises(ValidationError):
        s.ResetPasswordIn(token="t", new_password="short")


# --- ProjectIn ---
def test_cov_projectin_valid_and_trims():
    s = _S()
    p = s.ProjectIn(name="  Proj ", description="  d  ", color="#aabbcc")
    assert p.name == "Proj"
    assert p.description == "d"
    assert p.color == "#aabbcc"


def test_cov_projectin_default_color():
    s = _S()
    p = s.ProjectIn(name="Proj")
    assert p.color == "#c9764f"
    assert p.description == ""


def test_cov_projectin_short_name_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.ProjectIn(name="a")


def test_cov_projectin_bad_color_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.ProjectIn(name="Proj", color="red")
    with pytest.raises(ValidationError):
        s.ProjectIn(name="Proj", color="#fff")  # 3 hex not 6


# --- BugCreate — field + model validators ---
def test_cov_bugcreate_valid_defaults():
    s = _S()
    b = s.BugCreate(project_id=1, title="  A valid title ")
    assert b.title == "A valid title"
    assert b.item_type == "Bug"
    assert b.status == "New"
    assert b.priority == "Medium"
    assert b.environment == "DEV"


def test_cov_bugcreate_normalizes_choices():
    s = _S()
    b = s.BugCreate(project_id=1, title="Title here", item_type="task",
                    status="done", priority="high", environment="prod")
    assert b.item_type == "Task"
    assert b.status == "Done"
    assert b.priority == "High"
    assert b.environment == "PROD"


def test_cov_bugcreate_short_title_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="ab")


def test_cov_bugcreate_description_sanitized():
    # Description is stripped and flattened to plain text on creation.
    s = _S()
    b = s.BugCreate(project_id=1, title="Title here",
                    description="  <b>x</b><script>bad</script>  ")
    assert b.description == "x"
    assert "<script>" not in b.description


def test_cov_bugcreate_bad_item_type_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", item_type="NotARealType")


def test_cov_bugcreate_bad_status_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", status="Nonsense")


def test_cov_bugcreate_bad_priority_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", priority="Whenever")


def test_cov_bugcreate_bad_environment_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", environment="STAGING")


def test_cov_bugcreate_due_date_valid_and_empty():
    s = _S()
    b = s.BugCreate(project_id=1, title="Title here", due_date="2026-01-31")
    assert b.due_date == "2026-01-31"
    # An empty string is coerced to None.
    b2 = s.BugCreate(project_id=1, title="Title here", due_date="")
    assert b2.due_date is None


def test_cov_bugcreate_due_date_bad_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", due_date="31-01-2026")


def test_cov_bugcreate_assignee_dedup():
    s = _S()
    b = s.BugCreate(project_id=1, title="Title here", assignee_ids=[1, 1, 2, 2, 3])
    assert b.assignee_ids == [1, 2, 3]


def test_cov_bugcreate_status_invalid_for_type_raises():
    # With the universal status vocabulary, all statuses are valid for all
    # types. A status that is not in CANONICAL_STATUSES at all is still rejected.
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", item_type="Bug",
                    status="Not A Real Status At All")


def test_cov_bugcreate_status_valid_for_type_passes():
    s = _S()
    b = s.BugCreate(project_id=1, title="Title here", item_type="Requirement",
                    status="Approved")
    assert b.status == "Approved"


def test_cov_bugcreate_strip_desc_non_string_passthrough():
    # Pydantic rejects non-str before the validator runs, so we call the
    # validator directly to hit the isinstance guard.
    s = _S()
    assert s.BugCreate._strip_desc(None) is None


# --- BugUpdate — Optional everything (None early-returns) ---
def test_cov_bugupdate_all_none_passes():
    # Explicit None exercises each optional validator's early-return branch.
    s = _S()
    u = s.BugUpdate(project_id=None, title=None, description=None,
                    reporter_id=None, assignee_ids=None, item_type=None,
                    status=None, priority=None, environment=None,
                    due_date=None, event_id=None)
    assert u.title is None
    assert u.description is None
    assert u.item_type is None
    assert u.status is None
    assert u.priority is None
    assert u.environment is None
    assert u.due_date is None
    assert u.assignee_ids is None


def test_cov_bugupdate_values_validated():
    s = _S()
    u = s.BugUpdate(title="  New title ", item_type="task", status="blocked",
                    priority="low", environment="uat",
                    description="<b>k</b><script>x</script>",
                    assignee_ids=[2, 2, 5], due_date="2026-02-02")
    assert u.title == "New title"
    assert u.item_type == "Task"
    assert u.status == "Blocked"
    assert u.priority == "Low"
    assert u.environment == "UAT"
    assert "<script>" not in u.description
    assert u.assignee_ids == [2, 5]
    assert u.due_date == "2026-02-02"


def test_cov_bugupdate_short_title_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(title="ab")


def test_cov_bugupdate_bad_item_type_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(item_type="NotARealType")


def test_cov_bugupdate_bad_status_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(status="Nope")


def test_cov_bugupdate_bad_priority_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(priority="Sometime")


def test_cov_bugupdate_bad_environment_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(environment="LOCAL")


def test_cov_bugupdate_due_date_bad_raises():
    # A wrongly formatted date raises a validation error.
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(due_date="2026/02/02")


def test_cov_bugupdate_due_date_empty_is_none():
    # An empty string is coerced to None.
    s = _S()
    assert s.BugUpdate(due_date="").due_date is None


def test_cov_bugupdate_strip_desc_non_string_passthrough():
    # Call the validator directly to hit the isinstance guard.
    s = _S()
    assert s.BugUpdate._strip_desc(123) == 123


# --- CommentIn ---
def test_cov_commentin_valid_sanitized():
    s = _S()
    c = s.CommentIn(body="  <b>hello</b><script>x</script> ")
    assert "<b>hello</b>" in c.body
    assert "<script>" not in c.body


def test_cov_commentin_whitespace_only_raises():
    # Whitespace-only content with no image is rejected.
    s = _S()
    with pytest.raises(ValidationError):
        s.CommentIn(body="<p>   </p>")


def test_cov_commentin_image_only_allowed():
    # A comment whose only content is an image is accepted (no readable text required).
    s = _S()
    c = s.CommentIn(body='<img src="data:image/png;base64,AAAA" alt="s">')
    assert "<img" in c.body


def test_cov_commentin_empty_string_rejected_by_field():
    # Field(min_length=1) rejects an empty string before the validator runs.
    s = _S()
    with pytest.raises(ValidationError):
        s.CommentIn(body="")


def test_cov_commentin_non_string_body_raises():
    # The model field coerces/rejects non-str, so we call the validator
    # directly to hit the isinstance guard.
    s = _S()
    with pytest.raises(ValueError):
        s.CommentIn._strip(123)


# --- EventCreate ---
def test_cov_eventcreate_valid_trims_desc():
    # Name and description are stripped; manager_ids are deduplicated.
    s = _S()
    e = s.EventCreate(name="  Standup ", description="  notes  ",
                      scheduled_for="2026-03-03", manager_ids=[1, 1, 2])
    assert e.name == "Standup"
    assert e.description == "notes"
    assert e.scheduled_for == "2026-03-03"
    assert e.manager_ids == [1, 2]


def test_cov_eventcreate_short_name_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.EventCreate(name="a")


def test_cov_eventcreate_scheduled_empty_is_none():
    # An empty string or missing value is coerced to None.
    s = _S()
    assert s.EventCreate(name="Standup", scheduled_for="").scheduled_for is None
    assert s.EventCreate(name="Standup").scheduled_for is None


def test_cov_eventcreate_scheduled_bad_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.EventCreate(name="Standup", scheduled_for="03-03-2026")


def test_cov_eventcreate_strip_desc_non_string_passthrough():
    # Call the validator directly to hit the non-str passthrough.
    s = _S()
    assert s.EventCreate._strip_desc(None) is None


# --- EventUpdate  (Optional: None early-returns + dedup) ---
def test_cov_eventupdate_all_none_passes():
    # Explicit None exercises the None-return branch in each optional validator.
    s = _S()
    u = s.EventUpdate(name=None, description=None, scheduled_for=None,
                      manager_ids=None)
    assert u.name is None
    assert u.scheduled_for is None
    assert u.manager_ids is None


def test_cov_eventupdate_values_validated():
    # Values are stripped, date validated, and manager_ids deduplicated.
    s = _S()
    u = s.EventUpdate(name="  Sprint ", description="  d  ",
                      scheduled_for="2026-04-04", manager_ids=[3, 3, 4])
    assert u.name == "Sprint"
    assert u.description == "d"
    assert u.scheduled_for == "2026-04-04"
    assert u.manager_ids == [3, 4]


def test_cov_eventupdate_short_name_raises():
    # A short name raises; None takes the early-return branch.
    s = _S()
    with pytest.raises(ValidationError):
        s.EventUpdate(name="a")


def test_cov_eventupdate_scheduled_bad_raises():
    # A wrongly formatted date raises a validation error.
    s = _S()
    with pytest.raises(ValidationError):
        s.EventUpdate(scheduled_for="04/04/2026")


def test_cov_eventupdate_scheduled_empty_is_none():
    # An empty string is coerced to None.
    s = _S()
    assert s.EventUpdate(scheduled_for="").scheduled_for is None


def test_cov_eventupdate_strip_desc_non_string_passthrough():
    # Call the validator directly to hit the non-str passthrough.
    s = _S()
    assert s.EventUpdate._strip_desc(123) == 123


# --- Push schemas (simple Field constraints) ---
def test_cov_push_subscribe_valid_and_invalid():
    s = _S()
    ok = s.PushSubscribeIn(token="abc")
    assert ok.platform == "web"
    with pytest.raises(ValidationError):
        s.PushSubscribeIn(token="")  # Field(min_length=1)


def test_cov_push_unsubscribe_valid_and_invalid():
    s = _S()
    assert s.PushUnsubscribeIn(token="abc").token == "abc"
    with pytest.raises(ValidationError):
        s.PushUnsubscribeIn(token="")


# --- statuses_for_type helper ---
def test_cov_statuses_for_type_known_and_unknown():
    s = _S()
    assert s.statuses_for_type("Task") == s.STATUSES_BY_TYPE["Task"]
    # An unrecognised type falls back to the Bug status list.
    assert s.statuses_for_type("Mystery") == s.STATUSES_BY_TYPE["Bug"]
    assert s.statuses_for_type("") == s.STATUSES_BY_TYPE["Bug"]
