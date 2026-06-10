import re
from dataclasses import dataclass, field
from html import escape
from typing import Any, Literal


PacketType = Literal["service_inquiry", "automator_support", "education_interest", "community_contribution"]

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class PacketField:
    label: str
    value: str


@dataclass(frozen=True)
class HandoffPacket:
    source: str
    packet_type: PacketType
    title: str
    summary: str
    contact: dict[str, str] = field(default_factory=dict)
    fields: list[PacketField] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def build_contact_handoff_packet(role: str, name: str, email: str, fields: dict[str, Any]) -> HandoffPacket:
    packet_type = _contact_packet_type(role)
    clean_fields = _packet_fields(fields)
    return HandoffPacket(
        source="contact_form",
        packet_type=packet_type,
        title=_title(packet_type),
        summary=_contact_summary(packet_type, clean_fields),
        contact={"name": _clean(name, 200), "email": _clean(email, 320)},
        fields=clean_fields,
        next_steps=_next_steps(packet_type),
        tags=[_clean(role, 80), packet_type],
    )


def build_terminal_intake_handoff_packet(
    selected_mode: str,
    intake: dict[str, Any],
    *,
    contact: dict[str, str] | None = None,
) -> HandoffPacket:
    packet_type = _intake_packet_type(selected_mode, intake)
    clean_fields = _packet_fields({
        "objective": intake.get("objective", ""),
        "building": intake.get("building", ""),
        "maturity": intake.get("maturity", ""),
        "help_needed": intake.get("help_needed", ""),
    })
    return HandoffPacket(
        source="terminal_intake",
        packet_type=packet_type,
        title=_title(packet_type),
        summary=_intake_summary(clean_fields),
        contact={key: _clean(value, 320) for key, value in (contact or {}).items() if _clean(value, 320)},
        fields=clean_fields,
        next_steps=_next_steps(packet_type),
        tags=[_clean(selected_mode, 80), packet_type],
    )


def render_packet_text(packet: HandoffPacket) -> str:
    lines = [packet.title, "", f"Source: {packet.source}", f"Type: {packet.packet_type}"]
    if packet.summary:
        lines.extend(["", "Summary:", packet.summary])
    if packet.contact:
        lines.extend(["", "Contact:"])
        lines.extend(f"- {_label(key)}: {value}" for key, value in packet.contact.items())
    if packet.fields:
        lines.extend(["", "Details:"])
        lines.extend(f"- {field.label}: {field.value}" for field in packet.fields)
    if packet.next_steps:
        lines.extend(["", "Suggested next steps:"])
        lines.extend(f"- {step}" for step in packet.next_steps)
    return "\n".join(lines)


def render_packet_html(packet: HandoffPacket) -> str:
    field_rows = "".join(_row(field.label, field.value) for field in packet.fields)
    contact_rows = "".join(_row(_label(key), value) for key, value in packet.contact.items())
    next_steps = "".join(f"<li>{escape(step)}</li>" for step in packet.next_steps)
    return f"""
<html><head><meta charset='utf-8'></head><body style='background:#242128;color:#F5F5F5;font-family:Inter,Arial,sans-serif;padding:32px'>
  <h2 style='color:#DCD077;font-family:Rajdhani,Arial,sans-serif;margin:0 0 8px'>{escape(packet.title)}</h2>
  <p style='color:#aaa;margin:0 0 24px'>Contact intake</p>
  <p style='color:#F5F5F5;margin:0 0 20px'>{escape(packet.summary)}</p>
  <table cellpadding='0' cellspacing='0'>{contact_rows}{field_rows}</table>
  <h3 style='color:#D4BF91;margin:24px 0 8px'>Suggested next steps</h3>
  <ul style='color:#F5F5F5;margin:0;padding-left:20px'>{next_steps}</ul>
</body></html>
"""


def packet_to_dict(packet: HandoffPacket) -> dict[str, Any]:
    return {
        "source": packet.source,
        "packet_type": packet.packet_type,
        "title": packet.title,
        "summary": packet.summary,
        "contact": packet.contact,
        "fields": [{"label": field.label, "value": field.value} for field in packet.fields],
        "next_steps": packet.next_steps,
        "tags": packet.tags,
    }


def _contact_packet_type(role: str) -> PacketType:
    if role == "student":
        return "education_interest"
    if role == "automation_engineer":
        return "community_contribution"
    return "service_inquiry"


def _intake_packet_type(selected_mode: str, intake: dict[str, Any]) -> PacketType:
    text = " ".join(str(value).lower() for value in intake.values())
    if selected_mode == "educator" or any(term in text for term in ("learn", "teach", "walk me through", "student")):
        return "education_interest"
    if selected_mode == "automator" or any(term in text for term in ("debug", "workflow", "technical")):
        return "automator_support"
    return "service_inquiry"


def _title(packet_type: PacketType) -> str:
    return {
        "service_inquiry": "Service inquiry handoff packet",
        "automator_support": "Automator support handoff packet",
        "education_interest": "Education interest handoff packet",
        "community_contribution": "Community contribution handoff packet",
    }[packet_type]


def _contact_summary(packet_type: PacketType, fields: list[PacketField]) -> str:
    values = {field.label.lower(): field.value for field in fields}
    if packet_type == "service_inquiry":
        return _clean("; ".join(filter(None, [values.get("company"), values.get("timeline"), values.get("notes")])) or "New service inquiry.", 500)
    if packet_type == "education_interest":
        return _clean(values.get("learning topics") or values.get("learning notes") or "New education interest.", 500)
    return _clean(values.get("community interest") or values.get("tool recommendations") or "New community contribution interest.", 500)


def _intake_summary(fields: list[PacketField]) -> str:
    values = {field.label.lower(): field.value for field in fields}
    return _clean("; ".join(filter(None, [values.get("objective"), values.get("building"), values.get("help needed")])) or "New terminal intake.", 500)


def _next_steps(packet_type: PacketType) -> list[str]:
    if packet_type == "education_interest":
        return ["Reply with the best learning path or invite them into the educator flow.", "Capture topic, level, timeline, and preferred format."]
    if packet_type == "automator_support":
        return ["Ask for trigger system, target system, expected result, and failure point.", "Decide whether this is quick guidance or a scoped engagement."]
    if packet_type == "community_contribution":
        return ["Review fit for community/resource contribution.", "Ask for public repo, examples, or contribution boundaries if needed."]
    return ["Reply with a scoping question focused on systems, risk, and desired outcome.", "Route to service discovery if the need is qualified."]


def _packet_fields(fields: dict[str, Any]) -> list[PacketField]:
    return [PacketField(_label(key), _render_value(value)) for key, value in fields.items() if _render_value(value)]


def _render_value(value: Any) -> str:
    if isinstance(value, list):
        return _clean(", ".join(str(item) for item in value if str(item).strip()), 1000)
    if value is False or value is None:
        return ""
    return _clean(str(value), 2000)


def _label(key: str) -> str:
    return _clean(key.replace("_", " ").title(), 80)


def _row(label: str, value: str) -> str:
    return f"<tr><td style='padding:4px 12px 4px 0;color:#888;vertical-align:top'>{escape(label)}</td><td style='padding:4px 0;color:#F5F5F5'>{escape(value)}</td></tr>"


def _clean(value: str, limit: int) -> str:
    return _CONTROL_RE.sub("", value).strip()[:limit]