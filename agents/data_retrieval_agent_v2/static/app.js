const form = document.getElementById("queryForm");
const modelSelect = document.getElementById("modelSelect");
const queryInput = document.getElementById("queryInput");
const evidenceInput = document.getElementById("evidenceInput");
const evidenceError = document.getElementById("evidenceError");
const conversation = document.getElementById("conversation");
const submitButton = document.getElementById("submitButton");
const clearButton = document.getElementById("clearButton");
const stageOutputs = document.getElementById("stageOutputs");
const finalResult = document.getElementById("finalResult");
const statusBadge = document.getElementById("statusBadge");
const resultMessage = document.getElementById("resultMessage");
const downloadReportButton = document.getElementById("downloadReportButton");
const clarificationHint = document.getElementById("clarificationHint");
const clarificationAnswerPanel = document.getElementById("clarificationAnswerPanel");
const clarificationAnswer = document.getElementById("clarificationAnswer");

let pendingClarificationQuery = "";
let pendingClarificationStage = "";
let latestReport = null;

const defaultEvidence = {
  attribute_master_tables: {},
  distinct_database_values: {
    city_name: ["Pune"],
    property_type: ["residential"],
    transaction_category: ["sale"]
  },
  lookup_results: {}
};

evidenceInput.value = JSON.stringify(defaultEvidence, null, 2);

function appendBubble(role, text) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;
  conversation.appendChild(bubble);
  conversation.scrollTop = conversation.scrollHeight;
}

function setStatus(status, message) {
  statusBadge.className = `badge ${status}`;
  statusBadge.textContent = status.replaceAll("_", " ");
  resultMessage.textContent = message;
}

function parseEvidence() {
  evidenceError.classList.add("hidden");
  try {
    return JSON.parse(evidenceInput.value);
  } catch (error) {
    evidenceError.textContent = "Semantic context must contain valid JSON.";
    evidenceError.classList.remove("hidden");
    return null;
  }
}

function renderStages(data) {
  stageOutputs.replaceChildren();
  if (data.token_count) {
    stageOutputs.appendChild(createStageDetails("Total Token Count", data.token_count, true));
  }
  const outputs = data.stages || {};
  const reactIterations = data.react_iterations || [];
  const loopStagePattern = /^stage_3_[1-4](?:_iteration_\d+)?$/;

  Object.entries(outputs)
    .filter(([stageName]) => !reactIterations.length || !loopStagePattern.test(stageName))
    .forEach(([stageName, value]) => {
      stageOutputs.appendChild(createStageDetails(stageName.replaceAll("_", "."), value));
    });

  reactIterations.forEach((iteration) => {
    const group = document.createElement("section");
    group.className = "iteration-group";
    const heading = document.createElement("h3");
    heading.className = "iteration-heading";
    heading.textContent = `ReAct Iteration ${iteration.iteration}`;
    const stages = document.createElement("div");
    stages.className = "iteration-stages";

    [
      ["Stage 3.1 - SQL Review", iteration.sql_review_output],
      ["Stage 3.2 - SQL Probe", iteration.sql_probe_output],
      ["Stage 3.3 - SQL Observe", iteration.sql_observe_output],
      ["Stage 3.4 - SQL Fix", iteration.sql_fix_output]
    ].forEach(([stageLabel, value]) => {
      if (value) {
        stages.appendChild(createStageDetails(stageLabel, value, true));
      }
    });

    group.append(heading, stages);
    stageOutputs.appendChild(group);
  });
}

function getExecutionOutputs(probeOutput) {
  const results = probeOutput?.execution_results || {};
  const outputs = [];
  if (results.combined_sql?.applicable) {
    outputs.push({
      label: "Combined SQL",
      metricName: "",
      ...results.combined_sql
    });
  }
  (results.individual_sql_queries || []).forEach((result) => {
    outputs.push({
      label: result.metric_name ? `Metric: ${result.metric_name}` : "Individual SQL",
      metricName: result.metric_name || "",
      ...result
    });
  });
  return outputs;
}

function getApprovedSqlBlocks(reviewOutput) {
  const approved = reviewOutput?.approved_sql || {};
  const blocks = [];
  if (approved.combined_sql?.applicable && approved.combined_sql.sql_query) {
    blocks.push({
      label: "Combined SQL",
      sql: approved.combined_sql.sql_query
    });
  }
  (approved.individual_sql_queries || []).forEach((item) => {
    if (item.sql_query) {
      blocks.push({
        label: item.metric_name ? `Metric: ${item.metric_name}` : "Individual SQL",
        sql: item.sql_query
      });
    }
  });
  return blocks;
}

function createJsonBlock(value) {
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(value, null, 2);
  return pre;
}

function createOutputTable(rows) {
  const wrapper = document.createElement("div");
  wrapper.className = "result-table-wrap";
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "help";
    empty.textContent = "No rows to display.";
    wrapper.appendChild(empty);
    return wrapper;
  }

  const columns = Array.from(
    rows.reduce((set, row) => {
      Object.keys(row || {}).forEach((key) => set.add(key));
      return set;
    }, new Set())
  );
  const table = document.createElement("table");
  table.className = "result-table";
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  columns.forEach((column) => {
    const th = document.createElement("th");
    th.textContent = column;
    headerRow.appendChild(th);
  });
  thead.appendChild(headerRow);

  const tbody = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    columns.forEach((column) => {
      const td = document.createElement("td");
      const value = row?.[column];
      td.textContent = value === null || value === undefined ? "" : String(value);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });

  table.append(thead, tbody);
  wrapper.appendChild(table);
  return wrapper;
}

function renderFinalResult(data) {
  finalResult.replaceChildren();
  finalResult.classList.add("hidden");

  if (data.pipeline_status !== "completed") {
    return;
  }

  const sqlBlocks = getApprovedSqlBlocks(data.sql_review_output);
  const executionOutputs = getExecutionOutputs(data.sql_probe_output);
  const heading = document.createElement("h3");
  heading.textContent = "Final Result";
  const summary = document.createElement("p");
  summary.className = "final-summary";
  summary.textContent = data.sql_final_output?.layman_output || data.sql_probe_output?.final_probe_summary || data.message;

  const content = document.createElement("div");
  content.className = "final-result-grid";

  const sqlSection = document.createElement("section");
  sqlSection.className = "result-block";
  const sqlHeading = document.createElement("h4");
  sqlHeading.textContent = "Reviewed SQL";
  sqlSection.appendChild(sqlHeading);
  if (sqlBlocks.length) {
    sqlBlocks.forEach((block) => {
      const label = document.createElement("p");
      label.className = "result-label";
      label.textContent = block.label;
      const pre = document.createElement("pre");
      pre.textContent = block.sql;
      sqlSection.append(label, pre);
    });
  } else {
    const empty = document.createElement("p");
    empty.className = "help";
    empty.textContent = "No approved SQL was returned.";
    sqlSection.appendChild(empty);
  }

  const answerSection = document.createElement("section");
  answerSection.className = "result-block";
  const answerHeading = document.createElement("h4");
  answerHeading.textContent = "Plain Result";
  answerSection.appendChild(answerHeading);
  if (data.sql_final_output) {
    const layman = document.createElement("p");
    layman.className = "final-summary";
    layman.textContent = data.sql_final_output.layman_output || "";
    answerSection.appendChild(layman);

    const steps = data.sql_final_output.steps_of_arriving_at_output || [];
    if (steps.length) {
      const stepsHeading = document.createElement("p");
      stepsHeading.className = "result-label";
      stepsHeading.textContent = "Steps";
      const list = document.createElement("ol");
      steps.forEach((step) => {
        const item = document.createElement("li");
        item.textContent = step;
        list.appendChild(item);
      });
      answerSection.append(stepsHeading, list);
    }

    const insights = data.sql_final_output.insights || [];
    if (insights.length) {
      const insightsHeading = document.createElement("p");
      insightsHeading.className = "result-label";
      insightsHeading.textContent = "Insights";
      const list = document.createElement("ul");
      insights.forEach((insight) => {
        const item = document.createElement("li");
        item.textContent = insight;
        list.appendChild(item);
      });
      answerSection.append(insightsHeading, list);
    }
  } else {
    const empty = document.createElement("p");
    empty.className = "help";
    empty.textContent = "No final answer was returned.";
    answerSection.appendChild(empty);
  }

  const outputSection = document.createElement("section");
  outputSection.className = "result-block";
  const outputHeading = document.createElement("h4");
  outputHeading.textContent = "Raw Output";
  outputSection.appendChild(outputHeading);
  if (executionOutputs.length) {
    executionOutputs.forEach((result) => {
      const metadata = document.createElement("p");
      metadata.className = "result-label";
      metadata.textContent = `${result.label} | ${result.row_count || 0} row(s) | ${result.output_availability || "unknown"}`;
      const summary = document.createElement("p");
      summary.className = "final-summary";
      summary.textContent = result.output_summary || "";
      const rows = result.table_output || result.sample_output || [];
      outputSection.appendChild(metadata);
      outputSection.appendChild(summary);
      outputSection.appendChild(createOutputTable(rows));
      if (result.table_truncated) {
        const warning = document.createElement("p");
        warning.className = "table-warning";
        warning.textContent = "The table is truncated by SQL_PROBE_OUTPUT_LIMIT. Increase that setting to show more rows.";
        outputSection.appendChild(warning);
      }
    });
  } else {
    const empty = document.createElement("p");
    empty.className = "help";
    empty.textContent = "No execution output was returned.";
    outputSection.appendChild(empty);
  }

  content.append(answerSection, outputSection, sqlSection);
  finalResult.append(heading, summary, content);
  finalResult.classList.remove("hidden");
}

function createStageDetails(stageLabel, value, open = false) {
  const details = document.createElement("details");
  details.open = open || stageLabel === "stage.3";
  const summary = document.createElement("summary");
  summary.textContent = stageLabel;
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(value, null, 2);
  details.append(summary, pre);
  return details;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function buildWordReport(data, modelLabel) {
  const generatedAt = new Date().toLocaleString();
  const reactIterations = data.react_iterations || [];
  const loopStagePattern = /^stage_3_[1-4](?:_iteration_\d+)?$/;
  const stageSections = Object.entries(data.stages || {})
    .filter(([stageName]) => !reactIterations.length || !loopStagePattern.test(stageName))
    .map(([stageName, value]) => `
    <h2>${escapeHtml(stageName.replaceAll("_", "."))}</h2>
    <pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre>
  `).join("");
  const noStages = stageSections ? "" : "<p>No completed stage outputs were returned.</p>";
  const reactSections = reactIterations.map((iteration) => `
    <h2>ReAct Iteration ${escapeHtml(iteration.iteration)}</h2>
    ${[
      ["Stage 3.1 - SQL Review", iteration.sql_review_output],
      ["Stage 3.2 - SQL Probe", iteration.sql_probe_output],
      ["Stage 3.3 - SQL Observe", iteration.sql_observe_output],
      ["Stage 3.4 - SQL Fix", iteration.sql_fix_output]
    ].filter(([, value]) => value).map(([stageLabel, value]) => `
      <h3>${escapeHtml(stageLabel)}</h3>
      <pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre>
    `).join("")}
  `).join("");

  return `<!doctype html>
  <html>
    <head>
      <meta charset="utf-8">
      <title>SQL Agent Stage Report</title>
      <style>
        body { font-family: Arial, sans-serif; color: #19221f; }
        h1 { color: #125a47; }
        h2 { margin-top: 26px; border-bottom: 1px solid #d7ddd7; padding-bottom: 6px; }
        .metadata { border: 1px solid #d7ddd7; background: #f5f5f0; padding: 12px; }
        pre { white-space: pre-wrap; font-family: Consolas, monospace; font-size: 10pt; }
      </style>
    </head>
    <body>
      <h1>Real Estate SQL Agent - Stage Report</h1>
      <div class="metadata">
        <p><strong>Generated:</strong> ${escapeHtml(generatedAt)}</p>
        <p><strong>Model:</strong> ${escapeHtml(modelLabel)}</p>
        <p><strong>Status:</strong> ${escapeHtml(data.pipeline_status)}</p>
        <p><strong>Query:</strong> ${escapeHtml(data.query)}</p>
        <p><strong>Message:</strong> ${escapeHtml(data.message)}</p>
        ${data.token_count
          ? `<p><strong>Total Tokens:</strong> ${escapeHtml(data.token_count.total_tokens || 0)}</p>`
          : ""}
        ${data.clarification_question
          ? `<p><strong>Clarification:</strong> ${escapeHtml(data.clarification_question)}</p>`
          : ""}
      </div>
      ${data.token_count
        ? `<h2>Total Token Count</h2><pre>${escapeHtml(JSON.stringify(data.token_count, null, 2))}</pre>`
        : ""}
      ${stageSections}${noStages}
      ${reactSections}
    </body>
  </html>`;
}

function downloadWordReport(data, modelLabel) {
  const documentBody = buildWordReport(data, modelLabel);
  const reportBlob = new Blob(["\ufeff", documentBody], {
    type: "application/msword;charset=utf-8"
  });
  const reportUrl = URL.createObjectURL(reportBlob);
  const downloadLink = document.createElement("a");
  const dateStamp = new Date().toISOString().slice(0, 19).replaceAll(":", "-");
  downloadLink.href = reportUrl;
  downloadLink.download = `sql-agent-stage-report-${dateStamp}.doc`;
  document.body.appendChild(downloadLink);
  downloadLink.click();
  downloadLink.remove();
  URL.revokeObjectURL(reportUrl);
}

function showClarification(data) {
  const stage = data.stopped_at_stage ? ` (${data.stopped_at_stage.replaceAll("_", ".")})` : "";
  const question = data.clarification_question || "Please clarify the requested values.";
  appendBubble("assistant", `Clarification required${stage}: ${question}`);
  pendingClarificationQuery = data.query || queryInput.value.trim();
  pendingClarificationStage = data.stopped_at_stage || "";
  clarificationHint.textContent =
    pendingClarificationStage === "stage_2_1"
      ? "Answer below and update Semantic context JSON too if the database-approved value needs correction."
      : "Enter your answer below. The pipeline will rerun using your original query plus this clarification.";
  clarificationHint.classList.remove("hidden");
  clarificationAnswer.value = "";
  clarificationAnswerPanel.classList.remove("hidden");
  submitButton.textContent = "Submit Answer & Rerun";
  clarificationAnswer.focus();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const answer = clarificationAnswer.value.trim();
  let query = queryInput.value.trim();
  if (pendingClarificationQuery && !answer && query === pendingClarificationQuery) {
    clarificationHint.textContent = "Enter a clarification answer, or edit the complete query before rerunning.";
    clarificationHint.classList.remove("hidden");
    clarificationAnswer.focus();
    return;
  }
  const semanticContext = parseEvidence();
  if (!query || !semanticContext) {
    return;
  }

  if (pendingClarificationQuery && answer) {
    query = `${pendingClarificationQuery}\nClarification answer: ${answer}`;
    queryInput.value = query;
    appendBubble("user", answer);
  } else {
    appendBubble("user", query);
  }

  clarificationHint.classList.add("hidden");
  clarificationAnswerPanel.classList.add("hidden");
  pendingClarificationQuery = "";
  pendingClarificationStage = "";
  const modelLabel = modelSelect.options[modelSelect.selectedIndex].text;
  setStatus("running", `Running the sequential agent pipeline with ${modelLabel}...`);
  submitButton.disabled = true;
  modelSelect.disabled = true;
  submitButton.textContent = "Running...";

  try {
    const response = await fetch("/api/v1/sql/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        model: modelSelect.value,
        include_intermediate_stages: true,
        semantic_context: semanticContext
      })
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(JSON.stringify(data, null, 2));
    }

    renderFinalResult(data);
    renderStages(data);
    setStatus(data.pipeline_status, data.message);
    latestReport = { data, modelLabel };
    downloadReportButton.classList.remove("hidden");
    downloadWordReport(data, modelLabel);
    if (data.pipeline_status === "needs_clarification") {
      showClarification(data);
    } else if (data.pipeline_status === "completed") {
      const rowCount = getExecutionOutputs(data.sql_probe_output)
        .reduce((total, result) => total + (result.row_count || 0), 0);
      appendBubble("assistant", `SQL completed successfully and returned ${rowCount} row(s). The final SQL and sample output are shown above the stage panels.`);
    } else if (data.pipeline_status === "no_data") {
      appendBubble("assistant", "SQL executed, but no matching data was found. See SQL Observe evidence.");
    } else {
      appendBubble("assistant", data.message);
    }
  } catch (error) {
    setStatus("error", "The API request failed.");
    appendBubble("assistant", `Request failed: ${error.message}`);
  } finally {
    submitButton.disabled = false;
    modelSelect.disabled = false;
    if (!pendingClarificationQuery) {
      submitButton.textContent = "Run Pipeline";
    }
  }
});

downloadReportButton.addEventListener("click", () => {
  if (latestReport) {
    downloadWordReport(latestReport.data, latestReport.modelLabel);
  }
});

clearButton.addEventListener("click", () => {
  queryInput.value = "";
  finalResult.replaceChildren();
  finalResult.classList.add("hidden");
  stageOutputs.replaceChildren();
  clarificationHint.classList.add("hidden");
  clarificationAnswerPanel.classList.add("hidden");
  clarificationAnswer.value = "";
  pendingClarificationQuery = "";
  pendingClarificationStage = "";
  latestReport = null;
  downloadReportButton.classList.add("hidden");
  submitButton.textContent = "Run Pipeline";
  conversation.innerHTML = "";
  appendBubble(
    "assistant",
    "Start a new query. Include metric, location, property type, transaction category, and period."
  );
  setStatus("idle", "Completed stages will appear here as JSON.");
});
