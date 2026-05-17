from unittest.mock import patch

from core.runtime import GenerationConcurrencyPolicy


def test_generation_concurrency_defaults_match_parallelism_decision():
    policy = GenerationConcurrencyPolicy()

    assert policy.snapshot() == {
        "normal_parallel_limit": 3,
        "hard_parallel_limit": 5,
        "max_local_generations": 2,
        "max_cloud_generations": 3,
    }
    assert policy.can_start("local", active_total=2, active_local=1, active_cloud=1)
    assert not policy.can_start("local", active_total=2, active_local=2, active_cloud=0)
    assert not policy.can_start("premium", active_total=3, active_local=1, active_cloud=2)
    assert policy.can_start("premium", active_total=3, active_local=1, active_cloud=2, weighted_required=True)


def test_generation_concurrency_env_clamps_to_safe_bounds():
    with patch.dict("os.environ", {
        "GENERATION_NORMAL_PARALLEL_LIMIT": "4",
        "GENERATION_HARD_PARALLEL_LIMIT": "2",
        "GENERATION_MAX_LOCAL": "9",
        "GENERATION_MAX_CLOUD": "9",
    }, clear=False):
        policy = GenerationConcurrencyPolicy.from_env()

    assert policy.normal_parallel_limit == 4
    assert policy.hard_parallel_limit == 4
    assert policy.max_local_generations == 4
    assert policy.max_cloud_generations == 4