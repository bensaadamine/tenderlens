// TenderLens - the auditor's page: the mode, verdicts, commands, live output.

const MODE_NOTES = {
  rules: "No model is called: free and fast.",
  hybrid: "Rules + the M2 and M3 agents and M4's checks, live. Uses the model's daily quota.",
  replay: "The agents' answers come from the cache: no network, no quota, the same result every time.",
};
const CONFIRM_NOTES = {
  rules: "No model is called.",
  hybrid: "The M2 and M3 agents and M4's checks call the model (Groq). Its free quota " +
    "is about 200,000 tokens a day: one tender takes about a minute, a whole family can " +
    "take several days. A run that spends the quota stops; the next one resumes there.",
  replay: "The agents' answers come from the cache: no network, no quota, the same " +
    "result every time. A tender the agents have never seen stops the run.",
};
const MODE_NAMES = {rules: "Rules only", hybrid: "Hybrid, live", replay: "Hybrid, replayed"};

function mode() {
  const r = document.querySelector("input[name=mode]:checked");
  return r ? r.value : "rules";
}
function showMode() {
  const n = document.getElementById("mode-note");
  if (n) n.textContent = MODE_NOTES[mode()];
}
(function () {
  let saved = null;
  try { saved = localStorage.getItem("mode"); } catch (e) {}
  const r = saved && document.querySelector(`input[name=mode][value=${saved}]`);
  if (r) r.checked = true;
  document.querySelectorAll("input[name=mode]").forEach((el) =>
    el.addEventListener("change", () => {
      try { localStorage.setItem("mode", el.value); } catch (e) {}
      showMode();
    }));
  showMode();
})();

async function post(url, body) {
  const m = document.getElementById("msg");
  m.textContent = "";
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {})});
  const j = await r.json().catch(() => ({error: "The page server did not answer."}));
  if (!r.ok || j.error) { m.textContent = j.error || j.detail || "That did not work."; return null; }
  return j;
}

async function verdict(group, ref, v) {
  const n = document.getElementById("note");
  if (await post("/api/verdict", {group, reference: ref, verdict: v, note: n ? n.value : ""}))
    location.reload();
}

function ask(title, html, go, label) {
  const d = document.getElementById("ask");
  d.querySelector("h2").textContent = title;
  d.querySelector(".text").innerHTML = html;
  const b = d.querySelector(".go");
  b.textContent = label || "Start";
  b.onclick = () => { d.close(); go(); };
  d.showModal();
}

// Start a command in the chosen mode. Hybrid and anything with an
// explanation ask first; the page then follows the command's output.
function run(name, group, ref, title, html) {
  const m = mode();
  const go = async () => {
    const j = await post("/api/jobs/" + name, {group: group || "", reference: ref || "", mode: m});
    if (j) location.href = "/jobs/" + j.id;
  };
  if (m !== "hybrid" && !html) return go();
  ask(title || "Run with the agents?",
      (html || "") + `<p><b>Mode: ${MODE_NAMES[m]}.</b> ${CONFIRM_NOTES[m]}</p>`, go);
}

function askStart(d) {
  run("start", d.g, "", `Start ${d.g} ${d.name}?`,
    `<p>This downloads the documents of the family's <b>${d.n}</b> tenders, extracts their ` +
    `text, then runs every tender through the four components, oldest first.</p><ul>` +
    `<li><b>Time:</b> from tens of minutes to a few hours, depending on the number of ` +
    `documents. <b>Disk:</b> a few hundred MB (2510 took about 370 MB, 4110 about 550 MB).</li>` +
    `<li>It runs in the background, and verdicts wait until it ends. Stopping it is safe: ` +
    `starting it again picks up where it stopped.</li></ul>`);
}

// A command's output, followed while it runs.
(function () {
  const log = document.getElementById("log");
  if (!log || !log.dataset.live) return;
  log.scrollTop = log.scrollHeight;
  const t = setInterval(async () => {
    const j = await (await fetch("/api/jobs/" + log.dataset.job)).json();
    log.textContent = j.text;
    log.scrollTop = log.scrollHeight;
    if (!j.live) { clearInterval(t); location.reload(); }
  }, 2000);
})();
