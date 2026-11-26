// TenderLens - the auditor's page: verdicts, the trash, commands, live output.

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

async function askRules(ev) {
  ev.preventDefault();
  const q = document.getElementById("q").value, out = document.getElementById("answer");
  out.innerHTML = "<p class='small muted'>Searching the texts and asking the local model. A first answer can take a minute.</p>";
  const j = await post("/api/ask", {question: q});
  out.innerHTML = j ? j.html : "";
}

async function splitVerdict(group, v) {
  if (await post("/api/split_verdict", {group, verdict: v})) location.reload();
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

// Start a command: with the agents, unless the page offers the rules only
// (the quota is spent). An explanation, when given, is asked first; the page
// then follows the command's output.
function run(name, title, html, m) {
  const go = async () => {
    const j = await post("/api/jobs/" + name, {mode: m || "hybrid"});
    if (j) location.href = "/jobs/" + j.id;
  };
  if (!html) return go();
  ask(title, html, go);
}

const TRASH_ASK = {
  trash: ["Move to trash?", "<p>The tender leaves the queue and its requirements " +
    "leave the knowledge base: later tenders are no longer compared with it. " +
    "Restore brings it back.</p>", "Move to trash"],
  delete: ["Delete forever?", "<p>Every line of this tender is erased from the " +
    "knowledge base. This cannot be undone. The next runs will not read it again.</p>",
    "Delete forever"],
};

async function trash(group, ref, action) {
  const go = async () => {
    if (await post("/api/trash", {group, reference: ref, action})) location.reload();
  };
  const a = TRASH_ASK[action];
  if (!a) return go();
  ask(a[0], a[1], go, a[2]);
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
