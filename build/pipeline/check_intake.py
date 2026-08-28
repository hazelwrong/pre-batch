"""Check the material a person has to prepare, before anything is built.

Run this against a task directory as soon as the hand-prepared files are in
place. It reports what is missing and what is malformed, in the order it would
otherwise be discovered — which is late, one failure at a time, after a build.

    python3 pipeline/check_intake.py <task_id>
    python3 pipeline/check_intake.py --template <task_id>   # write blank forms

Nothing here judges quality. Whether the deliverable is genuinely a real work
product, whether the Chinese reads like a person wrote it, whether the reviewer
findings are substantive — those are the five gates in part three of the build
standard, and they need a reader.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taskdata as TD                                             # noqa: E402

HUMAN_FILES = ("coverage.json", "provenance.json", "source_inventory.json",
               "gold_revision.json", "gold_marking.json", "expert_profiles.json")

PROVENANCE_ROLES = ("reference", "deliverable", "index", "validation_evidence")
PROVENANCE_DEFAULTS = ("rights_holder", "version", "license", "usage_scope",
                       "contains_pii", "deidentification_note", "acquisition_date",
                       "usage_boundaries")
REVISION_FIELDS = ("source_material", "business_context", "deidentification", "scope")
LAYERS = ("general_review", "occupational_expert_review", "final_review")


def _fail(problems, where, message):
    problems.append("%s — %s" % (where, message))


def check_coverage(data, problems):
    for key in ("task_family", "workflow", "duplicate_group_id",
                "expected_expert_hours"):
        if not data.get(key):
            _fail(problems, "coverage.json", "缺 %s" % key)


def check_provenance(data, problems):
    prefix = data.get("source_record_prefix")
    if not prefix or not prefix.endswith("#"):
        _fail(problems, "provenance.json",
              "source_record_prefix 缺失或不以 # 结尾")
    defaults = data.get("defaults") or {}
    for key in PROVENANCE_DEFAULTS:
        if key not in defaults:
            _fail(problems, "provenance.json", "defaults 缺 %s" % key)
    roles = data.get("roles") or {}
    for role in PROVENANCE_ROLES:
        block = roles.get(role) or {}
        for key in ("source_type", "production_method", "drafted_by"):
            if not block.get(key):
                _fail(problems, "provenance.json", "roles.%s 缺 %s" % (role, key))
    deliverable = (roles.get("deliverable") or {}).get("source_type")
    accepted = tuple(((TD.policy().get("gold_source") or {})
                      .get("accepted_paths")) or ())
    if deliverable and deliverable not in accepted:
        _fail(problems, "provenance.json",
              "交付物的 source_type 是 %r，不在当前 policy.gold_source."
              "accepted_paths 中：%s" % (deliverable, "、".join(accepted)))


def check_source_inventory(data, problems):
    if not isinstance(data, list) or not data:
        _fail(problems, "source_inventory.json", "必须是非空数组")
        return
    for n, row in enumerate(data):
        for key in ("source_id", "source_type", "description", "adopted"):
            if key not in row:
                _fail(problems, "source_inventory.json", "第 %d 条缺 %s" % (n + 1, key))
        if row.get("adopted") is False and not row.get("rejection_reason"):
            _fail(problems, "source_inventory.json",
                  "第 %d 条未采用但没写 rejection_reason" % (n + 1))
        if row.get("adopted") is not False:
            for key in ("source_url", "license"):
                if not str(row.get(key) or "").strip():
                    _fail(problems, "source_inventory.json",
                          "第 %d 条已采用来源缺 %s" % (n + 1, key))


def check_expert_profiles(data, problems):
    rules = TD.policy().get("human_review") or {}
    count = int(rules.get("expert_profiles_required_per_task", 3))
    fields = rules.get("expert_profile_required_fields") or []
    if not isinstance(data, list) or len(data) != count:
        _fail(problems, "expert_profiles.json", "必须恰好有 %d 条画像" % count)
        return
    layers = set()
    mapping = {"通用审查": "general_review", "职业专家": "occupational_expert_review",
               "职业专家审查": "occupational_expert_review", "终审": "final_review"}
    for index, profile in enumerate(data, start=1):
        for field in fields:
            if not profile.get(field):
                _fail(problems, "expert_profiles.json",
                      "第 %d 条缺 %s" % (index, field))
        layer = profile.get("review_layer") or profile.get("expert_role")
        layers.add(mapping.get(layer, layer))
    expected = {"general_review", "occupational_expert_review", "final_review"}
    if layers != expected:
        _fail(problems, "expert_profiles.json", "三条画像必须分别覆盖三层审核")


def check_gold_revision(data, problems):
    record = data.get("revision_record") or {}
    for key in REVISION_FIELDS:
        if not record.get(key):
            _fail(problems, "gold_revision.json", "revision_record 缺 %s" % key)


def check_marking(data, rubric, codes, problems):
    for key in ("rubric_version", "marked_by", "marked_on", "method", "independence"):
        if not data.get(key):
            _fail(problems, "gold_marking.json", "缺 %s" % key)
    items = data.get("items") or []
    if not items:
        _fail(problems, "gold_marking.json", "items 为空——判断类条目没人评就不能说 gold 达阈值")
        return
    human_codes = [code for code, item in zip(codes, rubric) if not item.get("check")]
    marked = {row.get("code") for row in items}
    missing = [c for c in human_codes if c not in marked]
    if missing:
        _fail(problems, "gold_marking.json",
              "%d 个人工判断条目没有评分：%s" % (len(missing), ", ".join(missing[:8])))
    for row in items:
        if not str(row.get("evidence") or "").strip():
            _fail(problems, "gold_marking.json",
                  "%s 有分数但没写取证位置——没有定位理由的分数不是评分，是断言"
                  % row.get("code"))
        if row.get("awarded") is None:
            _fail(problems, "gold_marking.json", "%s 缺 awarded" % row.get("code"))
    if items and not any(row.get("shortfall") for row in items):
        _fail(problems, "gold_marking.json",
              "没有一条被判未达成。gold 满分说明评分标准是照着 gold 裁的")


def check_reviewers(data, problems):
    names = []
    for layer in LAYERS:
        entries = data.get(layer)
        entries = entries if isinstance(entries, list) else ([entries] if entries else [])
        if not entries:
            _fail(problems, "reviewers.json", "缺 %s 这一层" % layer)
            continue
        for entry in entries:
            for key in ("reviewer", "title", "date", "findings"):
                if not entry.get(key):
                    _fail(problems, "reviewers.json", "%s 缺 %s" % (layer, key))
            if entry.get("reviewer"):
                names.append(entry["reviewer"])
    if len(set(names)) < 3:
        _fail(problems, "reviewers.json",
              "三层复核必须由三个不同的人签署，当前只有 %d 个人：%s"
              % (len(set(names)), "、".join(sorted(set(names)))))
    experts = data.get("occupational_expert_review") or []
    experts = experts if isinstance(experts, list) else [experts]
    for expert in experts:
        if not expert.get("rubric_version_reviewed"):
            _fail(problems, "reviewers.json", "职业专家缺 rubric_version_reviewed")
        if not expert.get("items_reviewed"):
            _fail(problems, "reviewers.json", "职业专家缺 items_reviewed")
        if expert.get("credential_status") is None:
            _fail(problems, "reviewers.json", "职业专家缺 credential_status")


def check(task_id, tasks_root=None):
    root = os.path.join(tasks_root or TD.TASKS_ROOT, task_id)
    problems, present = [], []
    if not os.path.isdir(root):
        return ["任务目录不存在：%s" % root], []

    agent_side = ("rubric.json", "prompt.md", "task_meta.json",
                  "expected_values.json", "lineage.json")
    for name in agent_side:
        if not os.path.isfile(os.path.join(root, name)):
            _fail(problems, name, "还没有——这是 agent 侧产出，先跑 assemble_task.py")

    rubric = _read(root, "rubric.json") or []
    meta = _read(root, "task_meta.json") or {}
    codes = meta.get("item_codes") or ["R%02d" % (n + 1) for n in range(len(rubric))]

    checkers = {
        "coverage.json": lambda d: check_coverage(d, problems),
        "provenance.json": lambda d: check_provenance(d, problems),
        "source_inventory.json": lambda d: check_source_inventory(d, problems),
        "gold_revision.json": lambda d: check_gold_revision(d, problems),
        "gold_marking.json": lambda d: check_marking(d, rubric, codes, problems),
        "expert_profiles.json": lambda d: check_expert_profiles(d, problems),
    }
    for name in HUMAN_FILES:
        data = _read(root, name)
        if data is None:
            _fail(problems, name, "还没有")
            continue
        present.append(name)
        checkers[name](data)

    for entry in ((meta.get("privacy") or {}).get("expected_findings") or []):
        if not entry.get("justification"):
            _fail(problems, "task_meta.json",
                  "privacy.expected_findings 里有一条没写 justification——"
                  "声明扫描命中而不说明理由，等于把检查改绿")
    if meta and not meta.get("figure_pattern"):
        _fail(problems, "task_meta.json",
              "缺 figure_pattern——跨文件数字核对需要知道这一行的金额/数字长什么样，"
              "缺了那条 rubric 会直接报错")
    return problems, present


def _read(root, name):
    try:
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        if name.endswith(".md"):
            return None
        return None


def write_templates(task_id, tasks_root=None):
    root = os.path.join(tasks_root or TD.TASKS_ROOT, task_id)
    os.makedirs(root, exist_ok=True)
    blanks = {
        "coverage.json": {
            "task_family": "", "workflow": "", "duplicate_group_id": "",
            "template_family": "", "expected_expert_hours": "",
            "expected_expert_hours_definition":
                "具备该职业资格的人，仅凭 prompt 与参考文件做出交付物所需的时间。"
                "这是任务难度，不是我们花了多少工。"},
        "provenance.json": {
            "source_record_prefix": "supplier-work-records/<领域>/<年份>#",
            "defaults": {"rights_holder": "", "version": "v1", "license": "",
                         "usage_scope": "GDPval task environment and evaluation data",
                         "contains_pii": False, "deidentification_note": "",
                         "acquisition_date": "",
                         "usage_boundaries": {
                             "public_release": "not_authorized",
                             "internal_use": "authorized_for_client_controlled_gdpval",
                             "third_party_redistribution": "not_authorized",
                             "sublicensing": "not_authorized"},
                         "project_use_authorization": {
                             "status": "", "confirmed_by": "", "role": "",
                             "confirmed_at": "", "task_id": task_id, "scope": "",
                             "evidence_file": "", "evidence_sha256": "",
                             "usage_boundaries": {
                                 "public_release": "not_authorized",
                                 "internal_use": "authorized_for_client_controlled_gdpval",
                                 "third_party_redistribution": "not_authorized",
                                 "sublicensing": "not_authorized"}}},
            "roles": {
                "reference": {"source_type": "supplier_work_record",
                              "production_method": "", "drafted_by": ""},
                "deliverable": {"source_type": "real_input_and_real_deliverable",
                                "production_method": "", "drafted_by": "",
                                "revised_and_adopted_by": "",
                                "revision_evidence": "@gold_revision"},
                "index": {"source_type": "supplier_delivery_record",
                          "production_method": "supplier-assembled delivery record",
                          "drafted_by": ""},
                "validation_evidence": {"source_type": "supplier_validation_record",
                                        "production_method": "supplier quality-assurance record",
                                        "drafted_by": ""}}},
        "source_inventory.json": [
            {"source_id": "", "source_type": "", "description": "",
             "source_url": "", "adopted": True,
             "rejection_reason": None, "license": ""}],
        "gold_revision.json": {
            "date": "",
            "revision_record": {"source_material": "", "business_context": "",
                                "deidentification": "", "scope": ""}},
        "gold_marking.json": {
            "rubric_version": "", "marked_by": "", "marked_on": "",
            "independence": "自评。评分方与 gold 制作方同属供应商，非独立第三方评分。"
                            "据实声明，不作独立性主张。",
            "method": "",
            "items": [{"code": "", "score": 0, "awarded": 0, "evidence": "",
                       "shortfall": None}]},
        "expert_profiles.json": [{
            "expert_id": "E%02d" % index, "alias": "", "expert_role": role,
            "review_layer": layer, "required_industry": "",
            "required_occupation": "", "review_scope": "",
            "expert_profile": "", "strengths": [],
            "first_thought": "",
        } for index, (role, layer) in enumerate((
            ("通用审查", "general_review"),
            ("职业专家审查", "occupational_expert_review"),
            ("终审", "final_review")), start=1)],
    }
    written = []
    for name, blank in blanks.items():
        path = os.path.join(root, name)
        if os.path.isfile(path):
            continue
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(blank, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        written.append(name)
    return root, written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--template", action="store_true",
                        help="写出空白表单，不做检查")
    parser.add_argument("--tasks-root")
    args = parser.parse_args(argv)

    if args.template:
        root, written = write_templates(args.task_id, args.tasks_root)
        print("空白表单写入 %s" % root)
        for name in written:
            print("   +", name)
        if not written:
            print("   （已存在，未覆盖任何文件）")
        return 0

    problems, present = check(args.task_id, args.tasks_root)
    print("已就位：%s" % ("、".join(present) or "无"))
    if not problems:
        print("\n人工材料齐备。质量仍需人看——见建设规范第三部分的五道自审 gate。")
        return 0
    print("\n还差 %d 处：" % len(problems))
    for item in problems:
        print("  ·", item)
    return 1


if __name__ == "__main__":
    sys.exit(main())
