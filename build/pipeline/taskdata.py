"""Evaluator-only task data: the per-task inputs the validator needs and the
delivery must never contain.

Everything here lives under ``build/tasks/<task_id>/`` — outside the delivery
root on purpose. Execution logic for the rubric is evaluator material: shipping
it inside the package would hand the Agent the answer key, and the rubric
specification says in as many words that programmatic judgement logic does not
go into the delivered rubric.

    tasks/<task_id>/
    ├── task_meta.json      rubric_version and any per-task knobs
    ├── rubric.json         the delivered rubric items, as the rubric agent wrote them
    ├── rubric_pretty.txt   the reviewer-facing rendering of the same items
    ├── rubric_checks.json  {rubric_item_id: {code, type, params} | {human, reason}}
    └── expected_values.json  the verifier's independently recomputed figures

Of these only rubric.json and rubric_pretty.txt reach the delivery, as the two
rubric fields of the task record. The rest stay here.

Thresholds come from 产线规范/policy.json rather than from this module, so
the number in the acceptance report and the number in the specification are the
same number.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.dirname(HERE)
TASKS_ROOT = os.environ.get("GDPVAL_TASKS", os.path.join(BUILD, "tasks"))
POLICY_PATH = os.environ.get(
    "GDPVAL_POLICY",
    os.path.join(os.path.dirname(BUILD), "产线规范", "policy.json"))


def resolve_task_id(explicit=None):
    """Which task this run is about.

    Explicit argument, then GDPVAL_TASK_ID, then the single directory under
    tasks/. Guessing between several is refused: building the wrong task's
    delivery is not a failure anyone notices by reading the output.
    """
    task_id = explicit or os.environ.get("GDPVAL_TASK_ID")
    if task_id:
        return task_id
    try:
        candidates = sorted(name for name in os.listdir(TASKS_ROOT)
                            if os.path.isdir(os.path.join(TASKS_ROOT, name)))
    except OSError:
        candidates = []
    if len(candidates) == 1:
        return candidates[0]
    raise SystemExit(
        "set GDPVAL_TASK_ID: %d task directories under %s"
        % (len(candidates), TASKS_ROOT))


def _load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def policy():
    return _load(POLICY_PATH, {}) or {}


def threshold():
    """Acceptance threshold for gold-deliverable-eval, from policy."""
    return ((policy().get("gates") or {})
            .get("gold_deliverable_eval_threshold", 60))


class TaskData:
    """The evaluator-only side of one task. Missing files are an error, not a
    default: a validator that silently runs with no checks would report a clean
    delivery for a task nobody tested."""

    def __init__(self, task_id, root=None):
        self.task_id = task_id
        self.root = os.path.join(root or TASKS_ROOT, task_id)
        self.meta = _load(os.path.join(self.root, "task_meta.json"))
        self.expected = _load(os.path.join(self.root, "expected_values.json"), {})
        self.rubric = _load(os.path.join(self.root, "rubric.json"))
        self.marking = _load(os.path.join(self.root, "gold_marking.json"))
        self.lineage = _load(os.path.join(self.root, "lineage.json"))
        # The three manifests describe this task, so their content is task data.
        # Written as literals in the builder, they told the wrong story about a
        # package they no longer matched.
        self.coverage = _load(os.path.join(self.root, "coverage.json"), {})
        self.provenance = _load(os.path.join(self.root, "provenance.json"), {})
        self.source_inventory = _load(os.path.join(self.root, "source_inventory.json"), [])
        # Who reviewed this task, and what they objected to. Different people
        # and different adoption rounds per task, so it cannot sit beside the
        # code the way it used to.
        self.reviewers = _load(os.path.join(self.root, "reviewers.json"), {})
        self.gold_revision = _load(os.path.join(self.root, "gold_revision.json"), {})
        self.gold_provenance = _load(os.path.join(self.root, "gold_provenance.json"), {})
        self.policy_exceptions = _load(
            os.path.join(self.root, "policy_exceptions.json"), {})
        if self.meta is None or self.rubric is None:
            raise SystemExit(
                "missing evaluator data for task %s under %s — expected "
                "task_meta.json and rubric.json. Set GDPVAL_TASKS if the task "
                "directory lives elsewhere." % (task_id, self.root))

    @property
    def prompt(self):
        with open(os.path.join(self.root, "prompt.md"), encoding="utf-8") as fh:
            return fh.read().rstrip("\n")

    def column_map(self, sheet):
        """Column layout for a sheet. Held per sheet rather than per check so a
        check entry can name a sheet and the layout follows — which is what lets
        the delivered rubric carry its check params without column numbers."""
        maps = self.meta.get("column_maps") or {}
        return maps.get(sheet) or maps.get("*") or {}

    @property
    def rubric_pretty(self):
        path = os.path.join(self.root, "rubric_pretty.txt")
        with open(path, encoding="utf-8") as fh:
            return fh.read().rstrip("\n")

    @property
    def rubric_version(self):
        return self.meta.get("rubric_version", "unversioned")

    def codes_in_rubric_order(self, items):
        """R-codes for reporting. The delivered rubric does not carry them, so
        they come from the task, where the marking sheet and the reviewer
        records already use them. Generated positions would silently stop
        matching those records the first time an item was added or removed."""
        declared = self.meta.get("item_codes")
        if declared and len(declared) == len(items):
            return list(declared)
        return ["R%02d" % (n + 1) for n in range(len(items))]

    @staticmethod
    def unjudgeable(items):
        """Items that can be neither executed nor handed to a person: no check
        and no verification text. That combination is the one the delivery has
        already been rejected over — it reads as "we could not test it, so it
        passed"."""
        return [item["rubric_item_id"] for item in items
                if not item.get("check") and not str(item.get("verification") or "").strip()]
