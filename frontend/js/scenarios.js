const els = {
  scenarioForm: document.getElementById("scenario-form"),
  scenarioErrors: document.getElementById("scenario-errors"),
  scenarioSelect: document.getElementById("scenario-select"),
  controllerList: document.getElementById("controller-list"),
  runForm: document.getElementById("run-form"),
  runButton: document.getElementById("run-button"),
  runStatus: document.getElementById("run-status"),
  resultsBody: document.getElementById("results-body"),
  emptyMessage: document.getElementById("empty-message"),
};

async function request(method, path, body) {
  try {
    const response = await fetch("/api" + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, data };
  } catch (err) {
    return { ok: false, data: { error: "Could not reach the server" } };
  }
}

function showStatus(text, isError) {
  els.runStatus.textContent = text;
  els.runStatus.className = isError ? "error" : "";
}

function showErrors(messages) {
  els.scenarioErrors.replaceChildren();
  for (const message of messages) {
    const item = document.createElement("li");
    item.textContent = message;
    els.scenarioErrors.appendChild(item);
  }
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function fmt(value) {
  return value === null || value === undefined ? "-" : value.toFixed(2);
}

function renderRuns(runs) {
  els.resultsBody.replaceChildren();
  els.emptyMessage.hidden = runs.length > 0;
  const completed = runs.filter((r) => r.status === "completed");
  const bestWait = completed.length ? Math.min(...completed.map((r) => r.avg_wait_s)) : null;
  for (const run of [...runs].reverse()) {
    const row = document.createElement("tr");
    if (run.status === "completed" && run.avg_wait_s === bestWait) row.className = "best";
    row.appendChild(cell(run.id));
    row.appendChild(cell(run.controller));
    row.appendChild(cell(run.status, run.status === "failed" ? "failed" : ""));
    row.appendChild(cell(fmt(run.avg_wait_s)));
    row.appendChild(cell(fmt(run.max_wait_s)));
    row.appendChild(cell(fmt(run.avg_time_loss_s)));
    row.appendChild(cell(fmt(run.avg_trip_duration_s)));
    row.appendChild(cell(run.vehicles_completed === null ? "-" : run.vehicles_completed));
    els.resultsBody.appendChild(row);
  }
}

async function loadRuns() {
  const id = els.scenarioSelect.value;
  if (!id) {
    renderRuns([]);
    return;
  }
  const { ok, data } = await request("GET", "/runs?scenario_id=" + id);
  if (ok) renderRuns(data);
}

async function loadScenarios(selectedId) {
  const { ok, data } = await request("GET", "/scenarios");
  if (!ok) {
    showStatus("Could not load scenarios", true);
    return;
  }
  els.scenarioSelect.replaceChildren();
  for (const scenario of data) {
    const option = document.createElement("option");
    option.value = scenario.id;
    option.textContent =
      scenario.name + " (" + scenario.ew_vehs_per_hour + " / " + scenario.ns_vehs_per_hour + " veh/h)";
    els.scenarioSelect.appendChild(option);
  }
  if (selectedId) els.scenarioSelect.value = String(selectedId);
  await loadRuns();
}

async function loadControllers() {
  const { ok, data } = await request("GET", "/controllers");
  if (!ok) {
    showStatus("Could not load controllers", true);
    return;
  }
  els.controllerList.replaceChildren();
  for (const name of data.controllers) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = name;
    input.checked = true;
    label.appendChild(input);
    label.appendChild(document.createTextNode(name));
    els.controllerList.appendChild(label);
  }
}

async function createScenario(event) {
  event.preventDefault();
  const description = document.getElementById("description").value.trim();
  const body = {
    name: document.getElementById("name").value.trim(),
    description: description || null,
    ew_vehs_per_hour: Number(document.getElementById("ew").value),
    ns_vehs_per_hour: Number(document.getElementById("ns").value),
  };
  const { ok, data } = await request("POST", "/scenarios", body);
  if (!ok) {
    showErrors(data.errors || [data.error || "Could not create the scenario"]);
    return;
  }
  showErrors([]);
  els.scenarioForm.reset();
  await loadScenarios(data.id);
  showStatus("Scenario created: " + data.name, false);
}

async function runSelected(event) {
  event.preventDefault();
  const scenarioId = Number(els.scenarioSelect.value);
  const chosen = [...els.controllerList.querySelectorAll("input:checked")].map((i) => i.value);
  if (!scenarioId) {
    showStatus("Choose a scenario first", true);
    return;
  }
  if (chosen.length === 0) {
    showStatus("Select at least one controller", true);
    return;
  }
  els.runButton.disabled = true;
  const failures = [];
  try {
    for (let i = 0; i < chosen.length; i++) {
      showStatus("Running " + chosen[i] + " (" + (i + 1) + " of " + chosen.length + ")...", false);
      const { ok, data } = await request("POST", "/runs", {
        scenario_id: scenarioId,
        controller: chosen[i],
      });
      if (!ok) failures.push(chosen[i] + ": " + (data.error || "failed"));
      await loadRuns();
    }
  } finally {
    els.runButton.disabled = false;
  }
  if (failures.length) {
    showStatus("Some runs failed: " + failures.join("; "), true);
  } else {
    showStatus("Done", false);
  }
}

els.scenarioForm.addEventListener("submit", createScenario);
els.runForm.addEventListener("submit", runSelected);
els.scenarioSelect.addEventListener("change", loadRuns);

loadControllers();
loadScenarios();
