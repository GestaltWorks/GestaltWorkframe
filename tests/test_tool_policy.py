from gestaltworkframe.core.policy import ChatMode, UserIntent
from gestaltworkframe.core.tool_policy import (
    SERVICE_INQUIRY_CTA,
    WORKFLOW_PATTERN_SEARCH,
    allowed_tools_for_mode,
    retrieval_tool_for,
    tool_definitions_for_mode,
)


def test_service_mode_has_contact_handoff_tool_definition():
    tools = tool_definitions_for_mode(ChatMode.SERVICE)
    names = [tool.name for tool in tools]

    assert SERVICE_INQUIRY_CTA in names
    cta = next(tool for tool in tools if tool.name == SERVICE_INQUIRY_CTA)
    assert "contact" in cta.description.lower()
    assert cta.destructive is False


def test_workflow_troubleshooting_maps_to_workflow_pattern_tool():
    assert WORKFLOW_PATTERN_SEARCH in allowed_tools_for_mode(ChatMode.AUTOMATOR)
    assert (
        retrieval_tool_for(UserIntent.TROUBLESHOOTING, ChatMode.AUTOMATOR, "broken workflow")
        == WORKFLOW_PATTERN_SEARCH
    )


def test_library_access_maps_to_workflow_pattern_tool():
    assert (
        retrieval_tool_for(UserIntent.TECHNICAL_HELP, ChatMode.AUTOMATOR, "how do i access the library")
        == WORKFLOW_PATTERN_SEARCH
    )


def test_importable_example_request_maps_to_workflow_pattern_tool():
    assert (
        retrieval_tool_for(UserIntent.TECHNICAL_HELP, ChatMode.AUTOMATOR, "i need an example bundle to import")
        == WORKFLOW_PATTERN_SEARCH
    )


def test_sample_script_onboarding_request_maps_to_workflow_pattern_tool():
    assert (
        retrieval_tool_for(UserIntent.TECHNICAL_HELP, ChatMode.AUTOMATOR, "sample scripts for automation user onboarding")
        == WORKFLOW_PATTERN_SEARCH
    )
