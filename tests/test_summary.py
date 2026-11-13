from work_sheet.tasks import MAX_SUMMARY, output_list, truncate


def test_short_summary_is_kept():
    assert truncate("  已完成。\n") == "已完成。"


def test_truncate_at_last_line_break():
    first = "第一行" * 50  # 150 chars
    second = "新建 [[Gambler's fallacy]]" * 30
    out = truncate(f"{first}\n{second}")
    assert out == f"{first}\n…"


def test_truncate_without_line_break_cuts_hard():
    assert truncate("x" * 700) == "x" * MAX_SUMMARY + "\n…"


def test_stdout_fallback_keeps_last_lines():
    lines = [f"line {i:03d} " + "y" * 40 for i in range(30)]
    out = truncate("\n".join(lines), keep_end=True)
    kept = out.split("\n")
    assert kept[0] == "…" and kept[-1] == lines[-1]
    assert kept[1] in lines and len("\n".join(kept[1:])) <= MAX_SUMMARY


def test_output_list():
    assert output_list([], [], []) == ""
    assert output_list(["wiki/concepts/Kelly criterion.md", "wiki/assets/chart.png"], [], []) == (
        "\n\n新建：[[wiki/concepts/Kelly criterion|Kelly criterion]]、`wiki/assets/chart.png`")
    assert output_list([], ["wiki/index.md"], ["a.pdf", "b.md"]) == "\n\n未能采集：a.pdf、b.md\n修改：[[wiki/index|index]]"
