// Fill this in only if the server is started with NEMO_SERVICE_TOKEN set (required for any non-loopback host).
// It is not a secret: it is shipped to every visitor's browser. It only stops default drive-by requests, nothing more.
const API_TOKEN = "";

const headers = () => ({
  "Content-Type": "application/json",
  ...(API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {}),
});

const $ = (id) => document.getElementById(id);
const linesOf = (id) => $(id).value.split("\n").map((s) => s.trim()).filter(Boolean);

const MATCH_ORDER = { strong: 0, possible: 1, adjacent: 2, weak: 3, excluded: 4 };
const MATCH_LABEL = { strong: "Strong match", possible: "Possible match", adjacent: "Adjacent", weak: "Weak match", excluded: "Excluded" };

async function refreshQuota() {
  try {
    const res = await fetch("/quota", { headers: headers() });
    if (!res.ok) return;
    const q = await res.json();
    $("quota-remaining").textContent = q.remaining;
    $("quota-limit").textContent = q.limit;
    $("submit-btn").disabled = q.remaining <= 0;
    if (q.remaining <= 0 && q.retry_after_seconds) {
      $("submit-btn").textContent = `No searches left — try again in ${Math.ceil(q.retry_after_seconds / 60)} min`;
    } else {
      $("submit-btn").textContent = "Find jobs";
    }
  } catch {
    // quota display is a courtesy; a failed fetch just leaves the last-known value showing
  }
}

function showError(msg) {
  const el = $("form-error");
  el.textContent = msg;
  el.hidden = !msg;
}

function jobCard(j) {
  const match = j.match || "weak";
  const reasons = (j.reasons || []).slice(0, 4);
  const applyLink = j.application_link
    ? `<a class="apply" href="${j.application_link}" target="_blank" rel="noopener">Apply →</a>`
    : "";
  return `
    <div class="job">
      <div class="job-top">
        <div>
          <h3>${escapeHtml(j.title || "Untitled role")}</h3>
          <p class="company">${escapeHtml(j.company || "Unknown company")} · ${escapeHtml(j.location || "location not stated")}</p>
        </div>
        <span class="badge ${match}">${MATCH_LABEL[match] || match}</span>
      </div>
      <p class="job-meta">${escapeHtml(j.employment || "employment type not stated")} · posted ${escapeHtml(j.reported_age || "unknown")} ·
        freshness: ${escapeHtml(j.freshness || "unknown")}</p>
      ${reasons.length ? `<ul class="job-reasons">${reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>` : ""}
      ${applyLink}
    </div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderResults(res) {
  const jobs = [...(res.fresh_shortlist || []), ...(res.age_unconfirmed || [])]
    .sort((a, b) => (MATCH_ORDER[a.match] ?? 9) - (MATCH_ORDER[b.match] ?? 9));
  $("results-summary").textContent = jobs.length
    ? `${jobs.length} job(s) found across ${(res.cities || []).join(", ")}, stopped because: ${res.stop_reason || "done"}.`
    : `No matching jobs found this run (stopped because: ${res.stop_reason || "done"}). Try a wider "posting age" or different cities.`;
  $("results-list").innerHTML = jobs.map(jobCard).join("");
  $("results").hidden = false;
}

async function poll(runId) {
  const statusText = $("status-text");
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    const res = await fetch(`/runs/${runId}`, { headers: headers() });
    const body = await res.json();
    if (!res.ok) {
      statusText.textContent = `Error: ${body.error || "unknown error"}`;
      return;
    }
    if (body.state === "running") {
      statusText.textContent = `Still searching… (${body.requests_used ?? 0}/${body.request_budget ?? "?"} searches used so far)`;
      continue;
    }
    $("status").hidden = true;
    if (body.state === "failed") {
      showError(`Search failed: ${body.error || "unknown error"}`);
      return;
    }
    renderResults(body);
    refreshQuota();
    return;
  }
  statusText.textContent = "This is taking longer than expected — check back in a minute.";
}

$("search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  showError("");
  $("results").hidden = true;

  const experience = $("experience").value.trim();
  const desired_roles = linesOf("roles");
  if (!experience || !desired_roles.length) {
    showError("Fill in your experience and at least one desired role.");
    return;
  }
  const cities = [...document.querySelectorAll('input[name="city"]:checked')].map((c) => c.value);
  if (!cities.length) {
    showError("Pick at least one city.");
    return;
  }
  const body = {
    experience,
    desired_roles,
    must_haves: linesOf("must-haves"),
    nice_to_haves: linesOf("nice-to-haves"),
    exclusions: linesOf("exclusions"),
    cities,
    max_age_hours: Number($("max-age").value),
  };

  $("submit-btn").disabled = true;
  try {
    const res = await fetch("/runs", { method: "POST", headers: headers(), body: JSON.stringify(body) });
    const data = await res.json();
    if (res.status === 429) {
      showError(`${data.error}`);
      refreshQuota();
      return;
    }
    if (res.status === 409) {
      showError("Someone else's search is running right now — please try again in a minute.");
      return;
    }
    if (!res.ok) {
      showError(data.error || "Something went wrong.");
      return;
    }
    refreshQuota();
    $("status").hidden = false;
    $("status-text").textContent = "Starting…";
    await poll(data.run_id);
  } catch (err) {
    showError("Could not reach the server. Please try again.");
  } finally {
    refreshQuota();
  }
});

refreshQuota();
setInterval(refreshQuota, 30000);
