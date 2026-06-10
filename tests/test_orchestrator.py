from gestaltworkframe.core.orchestrator import Orchestrator
from gestaltworkframe.core.policy import AudienceSegment, ChatMode, CloudSpendPolicy, ConversationStage, OutputShape, ResponsePolicy, SearchPlan, ToneSignal, ToolExecutionMode, UserIntent, UserNeed
from gestaltworkframe.core.tool_policy import LESSON_CONCEPT_SEARCH, REFERENCE_SEARCH, WORKFLOW_PATTERN_SEARCH


def test_small_talk_stays_in_starting_mode_without_retrieval():
    decision = Orchestrator().decide("automator", "hello")

    assert decision.selected_mode == ChatMode.AUTOMATOR
    assert decision.intent == UserIntent.SMALL_TALK
    assert decision.retrieval_required is False
    assert decision.provider_tools == []
    assert decision.tool_execution_mode == ToolExecutionMode.DISABLED
    assert decision.cloud_allowed is False


def test_intake_gate_blocks_freeform_chat_until_quiz_complete():
    decision = Orchestrator().decide("automator", "Tell me anything", intake_complete=False)

    assert decision.stage == ConversationStage.INTAKE
    assert decision.answer_allowed is False
    assert decision.intake_required is True
    assert decision.allowed_tools == []
    assert decision.provider_tools == []
    assert decision.tool_execution_mode == ToolExecutionMode.DISABLED
    assert decision.cloud_allowed is False


def test_off_scope_request_redirects_without_tools_or_model_spend():
    decision = Orchestrator().decide("automator", "Write me a poem about vacation food")

    assert decision.stage == ConversationStage.REDIRECT
    assert decision.intent == UserIntent.OUT_OF_SCOPE
    assert decision.answer_allowed is False
    assert decision.allowed_tools == []
    assert decision.provider_tools == []
    assert decision.tool_execution_mode == ToolExecutionMode.DISABLED
    assert decision.cloud_allowed is False


def test_teach_me_routes_to_educator():
    decision = Orchestrator().decide("automator", "teach me how Automation workflows work")

    assert decision.selected_mode == ChatMode.EDUCATOR
    assert decision.intent == UserIntent.EDUCATION
    assert decision.retrieval_required is True
    assert decision.retrieval_tool == LESSON_CONCEPT_SEARCH


def test_technical_question_routes_back_to_automator():
    decision = Orchestrator().decide("pipeline", "How do I use CTX in the platform Jinja?")

    assert decision.selected_mode == ChatMode.AUTOMATOR
    assert decision.intent == UserIntent.TECHNICAL_HELP
    assert REFERENCE_SEARCH in decision.allowed_tools
    assert decision.provider_tools == []
    assert decision.tool_execution_mode == ToolExecutionMode.BACKEND_RETRIEVAL_ONLY
    assert decision.retrieval_tool == REFERENCE_SEARCH
    assert decision.frame.audience == AudienceSegment.PRACTITIONER
    assert decision.frame.need == UserNeed.IMPLEMENTATION_HELP


def test_model_tool_loop_is_explicit_opt_in():
    decision = Orchestrator(model_tool_loop_enabled=True).decide("automator", "How do I use CTX in the platform Jinja?")

    assert decision.tool_execution_mode == ToolExecutionMode.MODEL_TOOL_LOOP
    assert decision.provider_tools == [REFERENCE_SEARCH]
    assert decision.max_model_calls_per_turn == 2


def test_confused_technical_question_routes_to_educator():
    decision = Orchestrator().decide("automator", "I'm lost, I don't understand CTX in the platform")

    assert decision.selected_mode == ChatMode.EDUCATOR
    assert decision.intent == UserIntent.TECHNICAL_HELP
    assert decision.tone == ToneSignal.CONFUSED
    assert decision.retrieval_required is True
    assert decision.retrieval_tool == LESSON_CONCEPT_SEARCH


def test_workflow_troubleshooting_uses_workflow_pattern_tool():
    decision = Orchestrator().decide("automator", "This workflow is still broken")

    assert decision.retrieval_tool == WORKFLOW_PATTERN_SEARCH
    assert WORKFLOW_PATTERN_SEARCH in decision.allowed_tools


def test_importable_bundle_request_is_in_scope_workflow_search():
    decision = Orchestrator().decide("automator", "I need an example bundle to import")

    assert decision.selected_mode == ChatMode.AUTOMATOR
    assert decision.intent == UserIntent.TECHNICAL_HELP
    assert decision.retrieval_tool == WORKFLOW_PATTERN_SEARCH
    assert decision.frame.audience == AudienceSegment.PRACTITIONER
    assert decision.frame.need == UserNeed.RESOURCE_LOOKUP
    assert decision.frame.output_shape == OutputShape.RECOMMENDATION
    assert decision.frame.search_plan == SearchPlan.LOCAL_PLUS_PUBLIC
    assert decision.frame.task == "workflow_examples"


def test_pricing_question_routes_to_client_service_handoff_without_retrieval():
    decision = Orchestrator().decide("automator", "What is your pricing model and contract terms?")

    assert decision.selected_mode == ChatMode.SERVICE
    assert decision.intent == UserIntent.SERVICE_INQUIRY
    assert decision.retrieval_required is False
    assert decision.service_handoff_suggested is True
    assert decision.frame.audience == AudienceSegment.CLIENT
    assert decision.frame.need == UserNeed.PRICING_TERMS
    assert decision.frame.output_shape == OutputShape.DIRECT_ANSWER


def test_student_scenario_routes_to_socratic_tutor_shape():
    decision = Orchestrator().decide("automator", "Give me a scenario to practice Automation workflow design")

    assert decision.selected_mode == ChatMode.EDUCATOR
    assert decision.intent == UserIntent.EDUCATION
    assert decision.retrieval_required is True
    assert decision.frame.audience == AudienceSegment.STUDENT
    assert decision.frame.need == UserNeed.EDUCATION
    assert decision.frame.output_shape == OutputShape.SOCRATIC_LESSON
    assert decision.frame.task == "socratic_tutor"


def test_hesitation_routes_to_pipeline_discovery():
    decision = Orchestrator().decide("automator", "We're not sure what to automate or where to start")

    assert decision.selected_mode == ChatMode.SERVICE
    assert decision.selected_mode.value == "pipeline"
    assert decision.tone == ToneSignal.HESITANT
    assert decision.service_handoff_suggested is False


def test_service_intake_does_not_force_handoff_for_vague_first_turn():
    decision = Orchestrator().decide(
        "pipeline",
        "I need help",
        intake={
            "objective": "Explore automation support or consulting",
            "building": "Automation",
            "maturity": "Just starting",
            "help_needed": "Service Inquiry",
        },
    )

    assert decision.selected_mode == ChatMode.SERVICE
    assert decision.intent == UserIntent.SERVICE_INQUIRY
    assert decision.answer_allowed is True
    assert decision.service_handoff_suggested is False


def test_team_help_question_routes_to_service_discovery_without_retrieval():
    decision = Orchestrator().decide("automator", "How can you help?")

    assert decision.selected_mode == ChatMode.SERVICE
    assert decision.intent == UserIntent.SERVICE_INQUIRY
    assert decision.retrieval_required is False
    assert decision.service_handoff_suggested is False
    assert decision.frame.need == UserNeed.SERVICE_DISCOVERY


def test_legacy_sales_mode_aliases_to_pipeline_for_existing_sessions():
    decision = Orchestrator().decide("sales", "hello")

    assert decision.selected_mode == ChatMode.SERVICE
    assert decision.selected_mode.value == "pipeline"


def test_frustrated_troubleshooting_routes_to_service_without_cloud_by_default():
    decision = Orchestrator().decide("automator", "This is still broken and I already tried that")

    assert decision.selected_mode == ChatMode.SERVICE
    assert decision.selected_mode.value == "pipeline"
    assert decision.intent == UserIntent.TROUBLESHOOTING
    assert decision.tone == ToneSignal.FRUSTRATED
    assert decision.service_handoff_suggested is True
    assert decision.response_policy == ResponsePolicy.LOCAL_ONLY
    assert decision.cloud_allowed is False


def test_urgent_client_technical_issue_routes_to_service_handoff():
    decision = Orchestrator().decide("automator", "Production client workflow is down in Automation")

    assert decision.selected_mode == ChatMode.SERVICE
    assert decision.intent == UserIntent.TECHNICAL_HELP
    assert decision.tone == ToneSignal.URGENT
    assert decision.service_handoff_suggested is True
    assert decision.retrieval_required is True


def test_request_for_team_debugging_routes_to_service_handoff():
    decision = Orchestrator().decide("automator", "Can you debug this for me in Automation?")

    assert decision.selected_mode == ChatMode.SERVICE
    assert decision.intent == UserIntent.SERVICE_INQUIRY
    assert decision.service_handoff_suggested is True


def test_service_handoff_can_use_claude_only_when_team_policy_allows_it():
    policy = CloudSpendPolicy(claude_enabled=True, max_cloud_calls_per_turn=1)
    decision = Orchestrator(policy).decide("automator", "Can you build this workflow for us?")

    assert decision.selected_mode == ChatMode.SERVICE
    assert decision.response_policy == ResponsePolicy.LOCAL_THEN_CLAUDE_IF_HIGH_VALUE
    assert decision.cloud_allowed is True


def test_intake_answers_influence_unknown_first_turn():
    decision = Orchestrator().decide(
        "automator",
        "hello",
        intake={
            "objective": "Learn how automation/Automation works",
            "building": "understand workflows",
            "maturity": "Just starting",
            "help_needed": "Walk me through it so I understand",
        },
    )

    assert decision.selected_mode == ChatMode.EDUCATOR
    assert decision.intent == UserIntent.EDUCATION
    assert decision.intake["help_needed"] == "Walk me through it so I understand"


def test_need_to_intent_mapping_is_complete_and_lossy_in_the_expected_directions():
    """Pin the documented Need -> Intent mapping.

    UserNeed is the fine-grained turn classifier; UserIntent is the coarser
    bucketing Orchestrator._select_mode branches on. The mapping is documented
    in core.policy enum docstrings and must stay in sync with
    routing_frame.need_to_intent. Changing either side without updating the
    other will fail this test.
    """

    from gestaltworkframe.core.routing_frame import need_to_intent

    expected: dict[UserNeed, UserIntent] = {
        UserNeed.SMALL_TALK: UserIntent.SMALL_TALK,
        UserNeed.OUT_OF_SCOPE: UserIntent.OUT_OF_SCOPE,
        UserNeed.EDUCATION: UserIntent.EDUCATION,
        UserNeed.SERVICE_DISCOVERY: UserIntent.SERVICE_INQUIRY,
        UserNeed.PRICING_TERMS: UserIntent.SERVICE_INQUIRY,
        UserNeed.PROJECT_INTAKE: UserIntent.SERVICE_INQUIRY,
        UserNeed.TROUBLESHOOTING: UserIntent.TROUBLESHOOTING,
        UserNeed.RESOURCE_LOOKUP: UserIntent.TECHNICAL_HELP,
        UserNeed.IMPLEMENTATION_HELP: UserIntent.TECHNICAL_HELP,
        UserNeed.UNKNOWN: UserIntent.UNKNOWN,
    }
    # Every Need must have a mapping.
    assert set(expected.keys()) == set(UserNeed)
    for need, intent in expected.items():
        assert need_to_intent(need) is intent, f"need_to_intent({need}) should be {intent}"
