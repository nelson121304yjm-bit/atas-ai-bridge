"use strict";
import {api, byId, connectSession, errorText, jobIsActive, runJob, setText, showNotice} from "./ui.js";
import {initAnalysis, refreshReports, updateStatus} from "./analysis.js";
import {initReport, recoverReportJob} from "./report.js";
import {initJournal, refreshTrades, updateJournalStatus} from "./journal.js";
import {initSystem, refreshProfile} from "./system.js";
import {initSettings, recoveredSettingsTest, refreshSettings} from "./settings.js";

let lastJournalConnected = null;
let statusLoading = false;
let initialized = false;
let starting = null;
function navigate(id) {
  for (const section of document.querySelectorAll("main > .page")) section.hidden = section.id !== id;
  for (const button of document.querySelectorAll(".tab")) {
    const selected = button.dataset.tab === id;
    button.classList.toggle("is-active", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  byId(`${id}-title`)?.focus?.();
}

function watchJob(job) {
  runJob(job, async result => {
    if (result.kind === "settings_test") recoveredSettingsTest();
    else { await recoverReportJob(result); await refreshReports(); }
  }).catch(async error => { showNotice(errorText(error)); await refreshReports(); });
}

async function refreshStatus(adoptJob = false) {
  if (statusLoading) return null;
  statusLoading = true;
  try {
    const status = await api("/api/status");
    updateStatus(status);
    const journalChanged = updateJournalStatus(status.journal);
    const connected = status.journal?.connected === true;
    if (connected && (lastJournalConnected !== true || journalChanged)) await refreshTrades();
    lastJournalConnected = connected;
    if (adoptJob && status.active_job && !jobIsActive()) watchJob(status.active_job);
    return status;
  } catch (error) {
    setText("service", "本机服务无法连接");
    showNotice(errorText(error));
    return null;
  } finally { statusLoading = false; }
}

async function startSession() {
  try { await connectSession(); }
  catch (error) { setText("service", "无法连接本机服务"); showNotice(errorText(error)); return; }
  showNotice("");
  if (!initialized) {
    initialized = true;
    for (const button of document.querySelectorAll(".tab")) button.addEventListener("click", () => navigate(button.dataset.tab));
    byId("refresh-status").addEventListener("click", () => refreshStatus(true));
    initAnalysis();
    initReport(() => refreshProfile(false));
    initJournal(() => navigate("analysis"));
    initSystem();
    initSettings();
    setInterval(() => refreshStatus(true), 10000);
  }
  const results = await Promise.allSettled([refreshStatus(), refreshSettings(), refreshProfile(), refreshReports()]);
  for (const result of results) if (result.status === "rejected") showNotice(errorText(result.reason));
  const status = results[0].status === "fulfilled" ? results[0].value : null;
  if (status?.active_job) watchJob(status.active_job);
}

export function start() {
  if (starting) return starting;
  starting = startSession().finally(() => {
    starting = null;
    // Covers a native message arriving while the initial session GET is pending.
    if (window.reviewBootstrap?.hasToken?.()) start();
  });
  return starting;
}

window.addEventListener("review-bootstrap", start);
start();
