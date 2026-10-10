from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from hashlib import sha1
from pathlib import Path
from typing import Any

from app.models import Evidence, LicenseAssessment, OpportunityReport, Score


GITHUB_REPOSITORY = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def parse_repository_url(url: str) -> tuple[str, str]:
    match = GITHUB_REPOSITORY.match(url.strip())
    if not match:
        raise ValueError("请输入完整的 GitHub 仓库地址，例如 https://github.com/owner/repo")
    return match.group("owner"), match.group("repo")


def _request_json(url: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "OpenRadar-MVP",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


async def fetch_github_metadata(owner: str, repo: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        return await asyncio.to_thread(_request_json, url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError("没有找到这个公开仓库，请检查地址是否正确。") from exc
        if exc.code in {403, 429}:
            raise ValueError("GitHub API 当前触发限流，请稍后重试或配置 GITHUB_TOKEN。") from exc
        raise ValueError(f"GitHub 返回错误：HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError("暂时无法连接 GitHub，请检查网络后重试。") from exc


def _score_profile(metadata: dict[str, Any]) -> list[Score]:
    text = " ".join(
        [
            str(metadata.get("name", "")),
            str(metadata.get("description", "")),
            " ".join(metadata.get("topics") or []),
        ]
    ).lower()
    language = (metadata.get("language") or "").lower()
    stars = int(metadata.get("stargazers_count") or 0)
    maturity = min(18, max(3, len(str(stars)) * 3))

    enterprise_words = ("self-host", "database", "knowledge", "workflow", "monitor", "security", "analytics", "crm")
    saas_words = ("api", "platform", "automation", "agent", "collaboration", "dashboard")
    desktop_words = ("desktop", "editor", "client", "productivity", "offline")

    private_score = 48 + maturity + sum(5 for word in enterprise_words if word in text)
    implementation_score = 45 + maturity + (9 if language in {"python", "typescript", "go", "java"} else 4)
    saas_score = 42 + maturity + sum(4 for word in saas_words if word in text)
    desktop_score = 28 + maturity + sum(7 for word in desktop_words if word in text)

    def clamp(value: int) -> int:
        return max(18, min(91, value))

    return [
        Score(
            key="private_deployment",
            label="企业私有化",
            value=clamp(private_score),
            rationale="数据控制、权限与企业集成可能形成交付空间。",
        ),
        Score(
            key="implementation",
            label="部署与实施",
            value=clamp(implementation_score),
            rationale="安装、配置、升级和支持可以形成服务收入。",
        ),
        Score(
            key="saas",
            label="SaaS 托管",
            value=clamp(saas_score),
            rationale="持续使用场景存在，但需验证托管成本与差异化。",
        ),
        Score(
            key="desktop",
            label="桌面产品化",
            value=clamp(desktop_score),
            rationale="只有在本地化、易用性或离线需求明确时才优先。",
        ),
    ]


def build_heuristic_report(metadata: dict[str, Any], *, is_sample: bool = False) -> OpportunityReport:
    full_name = str(metadata.get("full_name") or "unknown/repository")
    repo_url = str(metadata.get("html_url") or f"https://github.com/{full_name}")
    scores = _score_profile(metadata)
    best = max(scores, key=lambda item: item.value)
    license_key = ((metadata.get("license") or {}).get("spdx_id") or "NOASSERTION").upper()
    if license_key in {"NOASSERTION", "OTHER", ""}:
        license_assessment = LicenseAssessment(
            status="red",
            detected="未识别",
            summary="没有检测到明确许可证。暂停商业推荐，先完成版权与许可证人工复核。",
            manual_review_required=True,
        )
        verdict = "watch"
        verdict_label = "先观察"
    elif license_key.startswith(("AGPL", "GPL", "LGPL", "MPL")):
        license_assessment = LicenseAssessment(
            status="yellow",
            detected=license_key,
            summary="许可证允许商业使用，但分发或网络服务可能带来开源义务，需要人工复核。",
            manual_review_required=True,
        )
        verdict = "worth_validating"
        verdict_label = "值得验证"
    else:
        license_assessment = LicenseAssessment(
            status="green",
            detected=license_key,
            summary="许可证初筛未发现明显阻断项，仍需检查依赖、NOTICE 与商标规则。",
            manual_review_required=False,
        )
        verdict = "worth_validating"
        verdict_label = "值得验证"

    description = str(metadata.get("description") or "仓库暂未提供项目描述")
    language = str(metadata.get("language") or "多语言")
    report_id = sha1(full_name.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return OpportunityReport(
        id=report_id,
        repository=full_name,
        repository_url=repo_url,
        description=description,
        language=language,
        stars=metadata.get("stargazers_count"),
        updated_at=metadata.get("updated_at"),
        analyzed_at=datetime.now(UTC),
        is_sample=is_sample,
        verdict=verdict,
        verdict_label=verdict_label,
        recommended_path=best.label,
        buyer="需要控制数据、减少实施风险的中小企业技术负责人",
        paid_reason="客户购买的不是代码，而是可运行的环境、企业能力、升级保障和明确责任。",
        opportunity_summary=f"优先把 {full_name} 验证为“{best.label}”方案，先通过访谈确认客户是否愿意为部署与持续维护付费。",
        scores=scores,
        license=license_assessment,
        paid_features=["中文化与安装向导", "身份认证与细粒度权限", "备份、审计与升级服务", "行业系统集成"],
        delivery_plan=["制作可重复部署的容器方案", "补齐管理员和运维文档", "建立版本兼容与回滚机制"],
        risks=["当前结论主要来自仓库公开元数据", "尚未验证目标客户的真实预算", "需进一步检查依赖许可证和项目商标"],
        evidence=[
            Evidence(
                label="项目定位",
                detail=description,
                source="GitHub 仓库描述",
                url=repo_url,
            ),
            Evidence(
                label="维护信号",
                detail=f"仓库最近更新时间：{metadata.get('updated_at') or '未知'}",
                source="GitHub 元数据",
                url=repo_url,
            ),
            Evidence(
                label="许可证",
                detail=f"GitHub 检测结果：{license_assessment.detected}",
                source="GitHub License API",
                url=f"{repo_url}/blob/HEAD/LICENSE",
            ),
        ],
        seven_day_plan=[
            "第 1 天：完成安装并记录全部失败点",
            "第 2-3 天：做出中文演示环境和一页价值说明",
            "第 4-5 天：访谈 3 位目标客户，验证现有替代方案与预算",
            "第 6 天：估算交付、维护和获客成本",
            "第 7 天：根据证据决定继续、观察或放弃",
        ],
        confidence=62 if not is_sample else 78,
    )


def _extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Pi 没有返回可解析的 JSON 报告。")
    return json.loads(text[start : end + 1])


async def analyze_with_pi(repository_url: str) -> OpportunityReport:
    pi_path = shutil.which("pi")
    if not pi_path:
        raise ValueError("已启用 Pi 模式，但系统中找不到 pi 命令。")

    owner, repo = parse_repository_url(repository_url)
    with tempfile.TemporaryDirectory(prefix="openradar-") as workdir:
        clone = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--depth",
            "1",
            repository_url,
            workdir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, clone_error = await asyncio.wait_for(clone.communicate(), timeout=60)
        if clone.returncode != 0:
            raise ValueError(f"克隆仓库失败：{clone_error.decode('utf-8', errors='ignore')[:200]}")

        skill_path = Path(__file__).parent.parent / ".pi" / "skills" / "analyze-open-source-opportunity" / "SKILL.md"
        prompt = (
            f"使用 {skill_path} 分析当前仓库 {owner}/{repo}。"
            "严格按照 references/report-schema.json 输出单个 JSON 对象，不要输出 Markdown 代码围栏。"
        )
        process = await asyncio.create_subprocess_exec(
            pi_path,
            "--mode",
            "json",
            "--no-session",
            prompt,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        if process.returncode != 0:
            raise ValueError(f"Pi 分析失败：{stderr.decode('utf-8', errors='ignore')[:240]}")
        lines = [json.loads(line) for line in stdout.decode("utf-8").splitlines() if line.strip()]
        messages = [item.get("message") for item in lines if item.get("type") == "message_end"]
        content = messages[-1].get("content", []) if messages else []
        text = "".join(part.get("text", "") for part in content if part.get("type") == "text")
        return OpportunityReport.model_validate(_extract_json(text))


async def analyze_repository(repository_url: str) -> OpportunityReport:
    if os.getenv("OPENRADAR_ANALYZER", "github") == "pi":
        return await analyze_with_pi(repository_url)
    owner, repo = parse_repository_url(repository_url)
    metadata = await fetch_github_metadata(owner, repo)
    return build_heuristic_report(metadata)


def analyzer_name() -> str:
    return "pi" if os.getenv("OPENRADAR_ANALYZER", "github") == "pi" else "github-heuristic"
