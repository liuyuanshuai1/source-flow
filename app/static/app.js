const state = { reports: [], stageTimer: null };

const elements = {
  form: document.querySelector("#analyzeForm"),
  input: document.querySelector("#repositoryUrl"),
  button: document.querySelector("#analyzeButton"),
  message: document.querySelector("#formMessage"),
  stages: document.querySelector("#analysisStages"),
  grid: document.querySelector("#opportunityGrid"),
  engine: document.querySelector("#engineStatus"),
  dialog: document.querySelector("#reportDialog"),
  report: document.querySelector("#reportContent"),
  close: document.querySelector("#closeReport"),
};

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function formatNumber(value) {
  if (value === null || value === undefined) return "未知";
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function setMessage(text, type = "normal") {
  elements.message.textContent = text;
  elements.message.classList.toggle("error", type === "error");
}

function renderCard(report) {
  return `
    <article class="opportunity-card">
      <div class="card-head">
        <p class="repo-name">${escapeHtml(report.repository)}${report.is_sample ? '<span class="sample-note">示例</span>' : ""}</p>
        <span class="verdict">${escapeHtml(report.verdict_label)}</span>
      </div>
      <h3 class="card-title">${escapeHtml(report.opportunity_summary)}</h3>
      <p class="card-copy">${escapeHtml(report.description)}</p>
      <div class="card-meta">
        <div class="path-label">
          <span>推荐商业路径</span>
          <strong>${escapeHtml(report.recommended_path)}</strong>
        </div>
        <button class="view-report" type="button" data-report-id="${escapeHtml(report.id)}">查看完整报告</button>
      </div>
    </article>`;
}

function renderGrid() {
  if (!state.reports.length) {
    elements.grid.innerHTML = '<p class="form-message">还没有报告。提交一个公开 GitHub 仓库开始分析。</p>';
    return;
  }
  elements.grid.innerHTML = state.reports.map(renderCard).join("");
}

function list(items) {
  return `<ul class="compact-list">${items.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function openReport(report) {
  const evidence = report.evidence.map(item => `
    <div class="evidence-item">
      <strong>${escapeHtml(item.label)}</strong>
      <p>${escapeHtml(item.detail)}</p>
      ${item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source)}</a>` : `<span>${escapeHtml(item.source)}</span>`}
    </div>`).join("");
  const scores = report.scores.map(score => `
    <div class="score-item">
      <span>${escapeHtml(score.label)}</span>
      <strong>${score.value}</strong>
      <p>${escapeHtml(score.rationale)}</p>
    </div>`).join("");

  elements.report.innerHTML = `
    <header class="report-header">
      <span class="report-kicker">${escapeHtml(report.verdict_label)} · 置信度 ${report.confidence}</span>
      <h2>${escapeHtml(report.repository)}</h2>
      <p class="report-summary">${escapeHtml(report.opportunity_summary)}</p>
    </header>
    <div class="report-layout">
      <div>
        <section class="report-section">
          <h3>商业判断</h3>
          <div class="decision-grid">
            <div class="decision-item"><span>推荐路径</span><strong>${escapeHtml(report.recommended_path)}</strong></div>
            <div class="decision-item"><span>目标买家</span><strong>${escapeHtml(report.buyer)}</strong></div>
            <div class="decision-item"><span>付费理由</span><strong>${escapeHtml(report.paid_reason)}</strong></div>
            <div class="decision-item"><span>项目信号</span><strong>${escapeHtml(report.language)} · ${formatNumber(report.stars)} Stars</strong></div>
          </div>
        </section>
        <section class="report-section"><h3>商业路径评分</h3><div class="score-list">${scores}</div></section>
        <section class="report-section"><h3>可以买什么</h3>${list(report.paid_features)}</section>
        <section class="report-section"><h3>证据</h3><div class="evidence-list">${evidence}</div></section>
      </div>
      <aside>
        <section class="report-section">
          <h3>许可证闸门</h3>
          <div class="license-box ${escapeHtml(report.license.status)}">
            <strong>${escapeHtml(report.license.detected)}</strong>
            <p>${escapeHtml(report.license.summary)}</p>
          </div>
        </section>
        <section class="report-section"><h3>交付准备</h3>${list(report.delivery_plan)}</section>
        <section class="report-section"><h3>主要风险</h3>${list(report.risks)}</section>
        <section class="report-section"><h3>七天验证计划</h3>${list(report.seven_day_plan)}</section>
      </aside>
    </div>`;
  elements.dialog.showModal();
}

function startStages() {
  elements.stages.hidden = false;
  const stages = [...elements.stages.querySelectorAll(".stage")];
  let index = 0;
  stages.forEach((stage, i) => stage.classList.toggle("active", i === 0));
  state.stageTimer = window.setInterval(() => {
    index = Math.min(index + 1, stages.length - 1);
    stages.forEach((stage, i) => stage.classList.toggle("active", i === index));
  }, 1400);
}

function stopStages() {
  window.clearInterval(state.stageTimer);
  elements.stages.hidden = true;
}

async function loadInitialData() {
  try {
    const [healthResponse, reportsResponse] = await Promise.all([
      fetch("/api/health"),
      fetch("/api/opportunities"),
    ]);
    if (!healthResponse.ok || !reportsResponse.ok) throw new Error("初始化失败");
    const health = await healthResponse.json();
    state.reports = await reportsResponse.json();
    elements.engine.textContent = health.analyzer === "pi" ? "Pi 深度分析已启用" : "GitHub 初筛模式";
    renderGrid();
  } catch (error) {
    elements.engine.textContent = "分析引擎离线";
    elements.grid.innerHTML = '<p class="form-message error">示例报告加载失败，请确认本地服务已启动。</p>';
  }
}

elements.form.addEventListener("submit", async event => {
  event.preventDefault();
  const repositoryUrl = elements.input.value.trim();
  if (!repositoryUrl.startsWith("https://github.com/")) {
    setMessage("请输入完整的 GitHub 仓库地址。", "error");
    elements.input.focus();
    return;
  }
  elements.button.disabled = true;
  elements.button.textContent = "正在分析";
  setMessage("正在读取仓库信号并生成报告，请保持页面打开。")
  startStages();
  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository_url: repositoryUrl }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "分析失败，请稍后重试。");
    state.reports = [payload, ...state.reports.filter(item => item.repository !== payload.repository)];
    renderGrid();
    openReport(payload);
    setMessage("报告已生成。重要商业与许可证结论仍建议人工复核。")
  } catch (error) {
    setMessage(error.message || "分析失败，请稍后重试。", "error");
  } finally {
    stopStages();
    elements.button.disabled = false;
    elements.button.textContent = "生成商机报告";
  }
});

elements.grid.addEventListener("click", event => {
  const button = event.target.closest("[data-report-id]");
  if (!button) return;
  const report = state.reports.find(item => item.id === button.dataset.reportId);
  if (report) openReport(report);
});

elements.close.addEventListener("click", () => elements.dialog.close());
elements.dialog.addEventListener("click", event => {
  if (event.target === elements.dialog) elements.dialog.close();
});

loadInitialData();
