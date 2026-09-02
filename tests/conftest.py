"""Shared Hypothesis configuration.

Two profiles, because the two things we run the suite for want different settings:

* `default` — what a developer and CI run. The deadline is off because building a
  `ClientProfile` runs pydantic validation and the first example of a session pays for
  import-time work; a per-example wall-clock deadline measures the machine, not the
  engine.
* `mutmut` — what a mutation run gets, selected on the `MUTANT_UNDER_TEST` environment
  variable mutmut sets in every test process it starts. It is derandomised and has the
  example database switched off so that a mutant's verdict is a property of the mutant
  rather than of which examples Hypothesis happened to draw or replay that run.

The example budget is deliberately the same in both. A smaller budget under mutation
finishes sooner, but it reports survivors that the suite as developers actually run it
does kill — measured, not guessed: at 40 examples three mutants survived that 200
examples kill. A survivor has to mean the tests are weak, not that the budget was.
"""

import os

from hypothesis import HealthCheck, settings

_SUPPRESSED = [HealthCheck.too_slow]

settings.register_profile(
    "default",
    deadline=None,
    max_examples=200,
    suppress_health_check=_SUPPRESSED,
)
settings.register_profile(
    "mutmut",
    deadline=None,
    max_examples=200,
    derandomize=True,
    database=None,
    suppress_health_check=_SUPPRESSED,
)
settings.load_profile("mutmut" if "MUTANT_UNDER_TEST" in os.environ else "default")
