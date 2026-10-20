"""AI 植物分析单元测试。

核心断言只有一条：**缺 Key / 调用失败时，默认绝不返回一份看起来正常的报告。**
"""
from pathlib import Path
from typing import Any, Dict

import pytest

import _bootstrap  # noqa: F401  必须在导入项目模块之前执行：把仓库根目录加入 sys.path
from ai.errors import AIError
from ai.plant_analyzer import PlantAnalyzer
from ai.providers import BaseProvider
from hardware.camera import Camera


def _photo(temp_dirs: Dict[str, Path]) -> Path:
    cam = Camera(str(temp_dirs["photo_dir"]), mock=True, warmup_sec=0)
    photo = cam.capture("garden")
    cam.close()
    return photo


class _FakeProvider(BaseProvider):
    api_style = "openai"

    def __init__(self, text: str = "", error: Exception | None = None,
                 model: str = "fake-vision-1") -> None:
        super().__init__("fake", model=model, api_key="k",
                         base_url="http://fake", supports_vision=True)
        self._text = text
        self._error = error

    def complete(self, **_kw: Any) -> str:
        if self._error is not None:
            raise self._error
        return self._text


def test_missing_key_fails_fast(mock_config: Dict[str, Any], temp_dirs: Dict[str, Path],
                                mock_env):
    """无 Key 且未开启降级：抛 AIError，不生成占位报告。"""
    cfg = dict(mock_config["ai"])
    cfg["allow_mock_fallback"] = False
    analyzer = PlantAnalyzer(cfg, str(temp_dirs["report_dir"]), allow_mock_fallback=False)
    assert analyzer.is_mock is True
    assert analyzer.error_kind == "no_api_key"

    with pytest.raises(AIError) as ei:
        analyzer.analyze(_photo(temp_dirs))
    assert ei.value.kind == "no_api_key"
    assert ei.value.hint


def test_allow_mock_fallback_marks_report_degraded(mock_config: Dict[str, Any],
                                                   temp_dirs: Dict[str, Path], mock_env):
    """显式打开降级：报告必须被标注，且评分置空。"""
    cfg = dict(mock_config["ai"])
    cfg["allow_mock_fallback"] = True
    analyzer = PlantAnalyzer(cfg, str(temp_dirs["report_dir"]), allow_mock_fallback=True)

    report = analyzer.analyze(_photo(temp_dirs))
    assert report.degraded is True
    assert report.score is None, "降级报告不能给出评分（假分数会顺着钉钉标题传出去）"
    assert "MOCK" in report.markdown
    assert report.report_path.exists()
    assert "整体观感" in report.markdown


def test_analyze_with_real_provider(monkeypatch, mock_config: Dict[str, Any],
                                    temp_dirs: Dict[str, Path], mock_env):
    """注入可用 provider：走真实路径、提取评分、不加横幅。"""
    from ai import plant_analyzer as pa

    fake = _FakeProvider(text="## 真实分析\n\n叶片健康。\n\n**综合健康评分：5 / 5**")
    monkeypatch.setattr(pa, "build_provider", lambda *a, **k: fake)

    analyzer = PlantAnalyzer(mock_config["ai"], str(temp_dirs["report_dir"]))
    report = analyzer.analyze(_photo(temp_dirs))

    assert report.degraded is False
    assert report.score == 5
    assert report.model == "fake-vision-1"
    assert "MOCK" not in report.markdown
    assert analyzer.last_error is None


def test_runtime_failure_raises_without_fallback(monkeypatch, mock_config: Dict[str, Any],
                                                 temp_dirs: Dict[str, Path], mock_env):
    """运行期失败且未开降级：异常必须向上抛出（由编排层转成钉钉告警）。"""
    from ai import plant_analyzer as pa

    fake = _FakeProvider(error=AIError("模型不支持图片", kind="vision_unsupported"))
    monkeypatch.setattr(pa, "build_provider", lambda *a, **k: fake)

    analyzer = PlantAnalyzer(mock_config["ai"], str(temp_dirs["report_dir"]),
                             allow_mock_fallback=False)
    with pytest.raises(AIError) as ei:
        analyzer.analyze(_photo(temp_dirs))
    assert ei.value.kind == "vision_unsupported"
    assert analyzer.last_error["kind"] == "vision_unsupported"


def test_runtime_failure_degrades_and_records_reason(monkeypatch,
                                                     mock_config: Dict[str, Any],
                                                     temp_dirs: Dict[str, Path], mock_env):
    """开启降级时，运行期失败要留下可追溯的原因。"""
    from ai import plant_analyzer as pa

    fake = _FakeProvider(error=AIError("触发限流", kind="rate_limited"))
    monkeypatch.setattr(pa, "build_provider", lambda *a, **k: fake)

    analyzer = PlantAnalyzer(mock_config["ai"], str(temp_dirs["report_dir"]),
                             allow_mock_fallback=True)
    report = analyzer.analyze(_photo(temp_dirs))

    assert report.degraded is True
    assert report.score is None
    assert "rate_limited" in report.degraded_reason
    assert "MOCK" in report.markdown
    assert analyzer.last_error["kind"] == "rate_limited"


def test_describe_exposes_state(monkeypatch, mock_config: Dict[str, Any],
                                temp_dirs: Dict[str, Path], mock_env):
    from ai import plant_analyzer as pa

    fake = _FakeProvider(text="ok\n\n综合健康评分：4 / 5")
    monkeypatch.setattr(pa, "build_provider", lambda *a, **k: fake)
    info = PlantAnalyzer(mock_config["ai"], str(temp_dirs["report_dir"])).describe()
    assert info["provider"] == "fake"
    assert info["model"] == "fake-vision-1"
    assert info["available"] is True
    assert info["supports_vision"] is True


def test_analyzer_extract_score():
    assert PlantAnalyzer._extract_score("综合健康评分：3 / 5") == 3
    assert PlantAnalyzer._extract_score("综合健康评分 5/5") == 5
    assert PlantAnalyzer._extract_score("没有评分") is None


def test_missing_photo_raises(mock_config: Dict[str, Any], temp_dirs: Dict[str, Path],
                              mock_env):
    analyzer = PlantAnalyzer(mock_config["ai"], str(temp_dirs["report_dir"]))
    with pytest.raises(FileNotFoundError):
        analyzer.analyze(temp_dirs["report_dir"] / "nope.jpg")


if __name__ == "__main__":
    _bootstrap.run(__file__)
