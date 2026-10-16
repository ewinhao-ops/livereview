"""整场环节结构：段落的中文场景名和内部 key 都要认（2026-10-04 修）。

以前整场总结把段落的中文名（scene_label）传给 analyze_scene_flow，而它只认 key，
结果整场每一段都显示成「通用」，每个人都被诊断成「全程没有开场留人 / 逼单促单」。
"""

from app.core.scene import analyze_scene_flow


def test_chinese_labels_are_recognized():
    flow = analyze_scene_flow(["开场留人", "产品讲款", "逼单促单", "互动宠粉", "通用"])
    assert flow["arc"] == ["开场留人", "产品讲款", "逼单促单", "互动宠粉", "通用"]
    joined = "；".join(flow["diagnostics"])
    assert "开场留人" not in joined
    assert "逼单促单" not in joined
    assert "互动宠粉" not in joined
    assert {d["scene"] for d in flow["distribution"]} == {"开场留人", "产品讲款", "逼单促单", "互动宠粉", "通用"}


def test_labels_and_keys_give_same_result():
    keys = ["opening", "product", "urge", "general", "product", "interaction"]
    labels = ["开场留人", "产品讲款", "逼单促单", "通用", "产品讲款", "互动宠粉"]
    assert analyze_scene_flow(labels) == analyze_scene_flow(keys)


def test_missing_scenes_still_diagnosed():
    flow = analyze_scene_flow(["产品讲款", "通用", "产品讲款"])
    joined = "；".join(flow["diagnostics"])
    assert "开场留人" in joined and "逼单促单" in joined


def test_unknown_and_empty_values():
    flow = analyze_scene_flow(["", "未分场景", "乱七八糟", "产品讲款"])
    # 空串、「未分场景」丢掉；认不出的算通用
    assert flow["segments"] == 2
    assert flow["arc"] == ["通用", "产品讲款"]
    assert analyze_scene_flow([]) == {}
    assert analyze_scene_flow(["", "未分场景"]) == {}
