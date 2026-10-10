from app.analyzer import build_heuristic_report


SAMPLE_REPOSITORIES = [
    {
        "full_name": "langgenius/dify",
        "html_url": "https://github.com/langgenius/dify",
        "description": "面向团队构建生成式 AI 应用的开源平台，示例报告用于演示产品交互。",
        "language": "TypeScript",
        "stargazers_count": 125000,
        "updated_at": "2026-08-11T18:30:00Z",
        "topics": ["self-hosted", "workflow", "agent", "platform"],
        "license": {"spdx_id": "OTHER"},
    },
    {
        "full_name": "maybe-finance/maybe",
        "html_url": "https://github.com/maybe-finance/maybe",
        "description": "个人财务管理应用，示例报告用于展示本地产品化与托管机会。",
        "language": "Ruby",
        "stargazers_count": 52000,
        "updated_at": "2026-08-10T09:15:00Z",
        "topics": ["self-hosted", "finance", "dashboard"],
        "license": {"spdx_id": "AGPL-3.0"},
    },
]


def sample_reports():
    return [build_heuristic_report(item, is_sample=True) for item in SAMPLE_REPOSITORIES]
