"""Describe the existing permission decision without changing how it is made."""

FIELDS = ("request_id", "input_present", "input_skip_permissions",
          "previous_skip_permissions", "effective_skip_permissions", "permission_source")


def permission_audit(data, existing, effective, *, inherited_source=None):
    # worker.sh currently synthesizes skip_permissions from its parent. That is
    # transport input, not an explicit permission choice by the CLI caller.
    present = "skip_permissions" in data and inherited_source is None
    source = inherited_source or ("request" if present else
                                  "existing_agent" if "skip_permissions" in (existing or {}) else "default")
    return {"request_id": data["request_id"], "input_present": present,
            "input_skip_permissions": data["skip_permissions"] if present else None,
            "previous_skip_permissions": existing.get("skip_permissions") if existing else None,
            "effective_skip_permissions": effective, "permission_source": source}
