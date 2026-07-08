"use strict";

const $ = (id) => document.getElementById(id);
const BRIDGE_MODE = new URLSearchParams(window.location.search).get("bridge") === "1" || window.location.port === "8765";

const ui = {
  statusText: $("statusText"),
  connectBtn: $("connectBtn"),
  disconnectBtn: $("disconnectBtn"),
  initBtn: $("initBtn"),
  baselineBtn: $("baselineBtn"),
  startBtn: $("startBtn"),
  stopBtn: $("stopBtn"),
  lcdToggleBtn: $("lcdToggleBtn"),
  sendBtn: $("sendBtn"),
  clearLogBtn: $("clearLogBtn"),
  commandInput: $("commandInput"),
  serialLog: $("serialLog"),
  canvas: $("eitCanvas"),
  frameStat: $("frameStat"),
  validStat: $("validStat"),
  invalidStat: $("invalidStat"),
  retryStat: $("retryStat"),
  p98Stat: $("p98Stat"),
  relL2Stat: $("relL2Stat"),
  electrodesInput: $("electrodesInput"),
  samplesInput: $("samplesInput"),
  settleInput: $("settleInput"),
  rateInput: $("rateInput"),
  ppLimitInput: $("ppLimitInput"),
  retriesInput: $("retriesInput"),
  driveGainInput: $("driveGainInput"),
  measGainInput: $("measGainInput"),
  baselineFramesInput: $("baselineFramesInput"),
  intervalInput: $("intervalInput"),
  fastModeInput: $("fastModeInput"),
  vmaxInput: $("vmaxInput"),
  minVmaxInput: $("minVmaxInput"),
  deadbandInput: $("deadbandInput"),
  gridInput: $("gridInput"),
  labelsInput: $("labelsInput"),
  rawLogInput: $("rawLogInput"),
  gestureBar: $("gestureBar"),
  gestureName: $("gestureName"),
  gestureConf: $("gestureConf"),
};

const state = {
  port: null,
  reader: null,
  writer: null,
  eventSource: null,
  readLoopAbort: false,
  lineQueue: [],
  lineWaiters: [],
  receiveBuffer: "",
  connected: false,
  running: false,
  busy: false,
  operationInFlight: false,
  pendingLcdToggle: false,
  templateNodes: null,
  lastFrame: null,
};

function numberValue(input, fallback) {
  const value = Number(input.value);
  return Number.isFinite(value) ? value : fallback;
}

function intValue(input, fallback, min, max) {
  const value = Math.round(numberValue(input, fallback));
  return Math.max(min, Math.min(max, value));
}

function settings() {
  return {
    electrodes: intValue(ui.electrodesInput, 8, 4, 32),
    samples: intValue(ui.samplesInput, 256, 1, 4096),
    settleMs: intValue(ui.settleInput, 20, 0, 1000),
    rate: intValue(ui.rateInput, 200000, 1000, 2000000),
    ppLimit: intValue(ui.ppLimitInput, 180, 0, 1023),
    retries: intValue(ui.retriesInput, 1, 0, 3),
    driveGain: intValue(ui.driveGainInput, 512, 0, 1023),
    measGain: intValue(ui.measGainInput, 6, 0, 1023),
    baselineFrames: intValue(ui.baselineFramesInput, 5, 1, 50),
    intervalMs: intValue(ui.intervalInput, 0, 0, 10000),
    fastMode: ui.fastModeInput.checked,
    vmax: Math.max(0, numberValue(ui.vmaxInput, 0)),
    minVmax: Math.max(1.0e-9, numberValue(ui.minVmaxInput, 1.0e-4)),
    deadband: Math.max(0, numberValue(ui.deadbandInput, 0)),
    gridSize: intValue(ui.gridInput, 180, 80, 320),
    labels: ui.labelsInput.checked,
  };
}

function reconCommand(fast = false) {
  const s = settings();
  return [
    fast ? "reconfast" : "recon",
    s.electrodes,
    s.samples,
    s.settleMs,
    s.rate,
    s.ppLimit,
    s.retries,
  ].join(" ");
}

function baselineCommand() {
  const s = settings();
  return [
    "reconbase",
    s.electrodes,
    s.baselineFrames,
    s.samples,
    s.settleMs,
    s.rate,
    s.ppLimit,
    s.retries,
  ].join(" ");
}

function setStatus(text, kind = "") {
  ui.statusText.textContent = text;
  ui.statusText.classList.toggle("is-ok", kind === "ok");
  ui.statusText.classList.toggle("is-error", kind === "error");
}

function setBusy(busy) {
  state.busy = busy;
  updateButtons();
}

function updateButtons() {
  const c = state.connected;
  const idle = c && !state.running && !state.busy;
  ui.connectBtn.disabled = c || state.busy;
  ui.disconnectBtn.disabled = !c || state.busy;
  ui.initBtn.disabled = !idle;
  ui.baselineBtn.disabled = !idle;
  ui.startBtn.disabled = !idle;
  ui.stopBtn.disabled = !state.running;
  ui.lcdToggleBtn.disabled = !c || state.busy || (state.operationInFlight && !state.running);
  ui.sendBtn.disabled = !idle;
}

function logLine(text, direction = "rx") {
  if (direction === "rx" && !ui.rawLogInput.checked && isNoisyFrameLine(text)) {
    return;
  }
  const prefix = direction === "tx" ? "> " : "< ";
  ui.serialLog.textContent += prefix + text + "\n";
  const maxLength = ui.rawLogInput.checked ? 80000 : 20000;
  if (ui.serialLog.textContent.length > maxLength) {
    ui.serialLog.textContent = ui.serialLog.textContent.slice(-maxLength);
  }
  ui.serialLog.scrollTop = ui.serialLog.scrollHeight;
}

function isNoisyFrameLine(text) {
  const line = text.trim();
  return (
    line.startsWith("RECONFAST_DS,") ||
    /^\d+,\s*[-+0-9.eE]+,\s*[-+0-9.eE]+,\s*[-+0-9.eE]+$/.test(line) ||
    /^\d+,\d+,\d+$/.test(line)
  );
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function connectSerial() {
  if (BRIDGE_MODE) {
    await connectBridge();
    return;
  }

  if (!("serial" in navigator)) {
    setStatus("当前浏览器不支持 Web Serial，请使用 Chromium/Chrome 并通过 localhost 打开", "error");
    return;
  }

  if (state.connected) {
    setStatus("串口已经连接", "ok");
    return;
  }

  setBusy(true);
  try {
    const selectedPort = await navigator.serial.requestPort();
    try {
      await selectedPort.open({ baudRate: 460800, dataBits: 8, stopBits: 1, parity: "none", flowControl: "none" });
    } catch (error) {
      state.port = null;
      if (String(error && error.message).toLowerCase().includes("already open")) {
        throw new Error("串口已被打开。请关闭其他使用该串口的 Chrome 标签页/串口程序，或在旧页面点击“断开”后再连接。");
      }
      throw error;
    }
    state.port = selectedPort;
    state.writer = state.port.writable.getWriter();
    state.readLoopAbort = false;
    state.connected = true;
    state.templateNodes = null;
    readLoop();
    setStatus("已连接 460800 baud", "ok");
  } catch (error) {
    setStatus(`连接失败: ${error.message}`, "error");
  } finally {
    setBusy(false);
  }
}

async function connectBridge() {
  if (state.connected) {
    setStatus("桥接服务已经连接", "ok");
    return;
  }

  setBusy(true);
  try {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    state.lineQueue = [];
    state.receiveBuffer = "";
    state.eventSource = new EventSource("/events");
    state.eventSource.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type === "rx") {
        queueLine(payload.line);
        if (payload.line.trim()) {
          logLine(payload.line, "rx");
        }
      } else if (payload.type === "tx") {
        logLine(payload.line, "tx");
      } else if (payload.type === "status") {
        setStatus(payload.text, "ok");
      } else if (payload.type === "gesture") {
        showGesture(payload.label, payload.confidence, payload.all_probas);
      } else if (payload.type === "error") {
        setStatus(`桥接错误: ${payload.text}`, "error");
      }
    };
    state.eventSource.onerror = () => {
      if (state.connected) {
        setStatus("桥接事件流断开", "error");
      }
      state.connected = false;
      updateButtons();
    };
    await new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => reject(new Error("连接桥接服务超时")), 3000);
      state.eventSource.onopen = () => {
        window.clearTimeout(timer);
        resolve();
      };
    });
    state.connected = true;
    state.templateNodes = null;
    setStatus("已连接本机串口桥", "ok");
  } catch (error) {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    setStatus(`桥接连接失败: ${error.message}`, "error");
  } finally {
    setBusy(false);
  }
}

async function disconnectSerial() {
  if (BRIDGE_MODE) {
    disconnectBridge();
    return;
  }

  state.running = false;
  setBusy(true);
  try {
    state.readLoopAbort = true;
    resolveAllWaiters(null);
    if (state.reader) {
      try {
        await state.reader.cancel();
      } catch (_) {
        /* ignored */
      }
    }
    if (state.writer) {
      state.writer.releaseLock();
      state.writer = null;
    }
    if (state.port) {
      await state.port.close();
      state.port = null;
    }
  } catch (error) {
    setStatus(`断开失败: ${error.message}`, "error");
  } finally {
    state.connected = false;
    state.reader = null;
    state.lineQueue = [];
    state.lineWaiters = [];
    setStatus("未连接");
    setBusy(false);
  }
}

function disconnectBridge() {
  state.running = false;
  state.connected = false;
  resolveAllWaiters(null);
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  setStatus("桥接已断开");
  updateButtons();
}

async function readLoop() {
  const decoder = new TextDecoder();
  try {
    while (state.port && state.port.readable && !state.readLoopAbort) {
      state.reader = state.port.readable.getReader();
      try {
        while (!state.readLoopAbort) {
          const { value, done } = await state.reader.read();
          if (done) {
            break;
          }
          if (value) {
            receiveText(decoder.decode(value, { stream: true }));
          }
        }
      } finally {
        state.reader.releaseLock();
        state.reader = null;
      }
    }
  } catch (error) {
    if (!state.readLoopAbort) {
      await handleSerialLost(error);
    }
  }
}

async function handleSerialLost(error) {
  state.running = false;
  state.connected = false;
  state.readLoopAbort = true;
  resolveAllWaiters(null);

  if (state.writer) {
    try {
      state.writer.releaseLock();
    } catch (_) {
      /* ignored */
    }
    state.writer = null;
  }

  if (state.port) {
    try {
      await state.port.close();
    } catch (_) {
      /* The OS or browser may have already invalidated the device handle. */
    }
    state.port = null;
  }

  setStatus(`串口读取失败: ${error.message}。请确认板子未复位/掉线，然后重新连接。`, "error");
  updateButtons();
}

function receiveText(text) {
  state.receiveBuffer += text;
  while (true) {
    const index = state.receiveBuffer.indexOf("\n");
    if (index < 0) {
      break;
    }
    let line = state.receiveBuffer.slice(0, index);
    state.receiveBuffer = state.receiveBuffer.slice(index + 1);
    if (line.endsWith("\r")) {
      line = line.slice(0, -1);
    }
    queueLine(line);
    if (line.trim()) {
      logLine(line, "rx");
    }
  }
}

function queueLine(line) {
  if (state.lineWaiters.length > 0) {
    const waiter = state.lineWaiters.shift();
    window.clearTimeout(waiter.timer);
    waiter.resolve(line);
    return;
  }
  state.lineQueue.push(line);
  if (state.lineQueue.length > 512) {
    state.lineQueue.splice(0, state.lineQueue.length - 512);
  }
}

function readLine(timeoutMs) {
  if (state.lineQueue.length > 0) {
    return Promise.resolve(state.lineQueue.shift());
  }
  return new Promise((resolve) => {
    const waiter = {
      resolve,
      timer: window.setTimeout(() => {
        const index = state.lineWaiters.indexOf(waiter);
        if (index >= 0) {
          state.lineWaiters.splice(index, 1);
        }
        resolve(null);
      }, timeoutMs),
    };
    state.lineWaiters.push(waiter);
  });
}

function resolveAllWaiters(value) {
  for (const waiter of state.lineWaiters.splice(0)) {
    window.clearTimeout(waiter.timer);
    waiter.resolve(value);
  }
}

async function writeCommand(command) {
  if (BRIDGE_MODE) {
    const trimmed = command.trim();
    if (!trimmed) {
      return;
    }
    const response = await fetch("/write", {
      method: "POST",
      headers: { "Content-Type": "text/plain; charset=utf-8" },
      body: trimmed,
    });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        message = payload.error || message;
      } catch (_) {
        /* ignored */
      }
      throw new Error(message);
    }
    return;
  }

  if (!state.writer) {
    throw new Error("serial port is not connected");
  }
  const encoder = new TextEncoder();
  const trimmed = command.trim();
  logLine(trimmed, "tx");
  for (const ch of trimmed) {
    await state.writer.write(encoder.encode(ch));
    await sleep(3);
  }
  await sleep(50);
  for (const ch of "\r\n\r") {
    await state.writer.write(encoder.encode(ch));
    await sleep(20);
  }
}

async function drainIdle(idleMs = 150, maxMs = 1000) {
  const endAt = performance.now() + maxMs;
  let idleAt = performance.now() + idleMs;
  while (performance.now() < endAt) {
    const line = await readLine(Math.max(1, Math.min(50, idleAt - performance.now())));
    if (line === null) {
      if (performance.now() >= idleAt) {
        return;
      }
      continue;
    }
    if (line.trim()) {
      idleAt = performance.now() + idleMs;
    }
  }
}

function cleanLine(line) {
  const markers = [
    "RECONDUMP,",
    "RECONBASE_BEGIN,",
    "RECONBASE_FRAME,",
    "RECONBASE_DONE,",
    "RECON_BEGIN,",
    "RECONFAST_BEGIN,",
    "RECON_SUMMARY,",
    "RECONFAST_DS,",
    "RECONFAST_DONE",
    "RECON_DONE",
    "ERR:",
    "bad command",
    "node,x,y,ds",
    "power ok",
    "gain drive=",
    "version ",
  ];
  for (const marker of markers) {
    const index = line.indexOf(marker);
    if (index >= 0) {
      return line.slice(index).trim();
    }
  }
  const stripped = line.trim();
  if (/^\d+,/.test(stripped)) {
    return stripped;
  }
  return stripped;
}

async function runSimpleCommand(command, accept, timeoutMs = 5000) {
  await drainIdle();
  await writeCommand(command);
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    const line = await readLine(Math.max(1, deadline - performance.now()));
    if (line === null) {
      break;
    }
    const cleaned = cleanLine(line);
    if (!cleaned) {
      continue;
    }
    if (cleaned.startsWith("ERR:") || cleaned.startsWith("bad command")) {
      throw new Error(cleaned);
    }
    if (typeof accept === "string" && cleaned.startsWith(accept)) {
      return cleaned;
    }
    if (typeof accept === "function" && accept(cleaned)) {
      return cleaned;
    }
  }
  throw new Error(`等待响应超时: ${command}`);
}

async function initBoard() {
  setBusy(true);
  try {
    const s = settings();
    await runSimpleCommand("p 1 0 0", "power ok");
    await runSimpleCommand(`g ${s.driveGain} ${s.measGain}`, "gain drive=");
    const dump = await runSimpleCommand("recondump", "RECONDUMP,");
    setStatus(`初始化完成 ${dump}`, "ok");
  } catch (error) {
    setStatus(`初始化失败: ${error.message}`, "error");
  } finally {
    setBusy(false);
  }
}

async function captureBaseline() {
  if (state.operationInFlight || state.running) {
    return;
  }
  state.operationInFlight = true;
  setBusy(true);
  try {
    await doBaseline();
  } catch (error) {
    setStatus(`基线失败: ${error.message}`, "error");
  } finally {
    state.operationInFlight = false;
    setBusy(false);
  }
}

async function doBaseline() {
  const command = baselineCommand();
  const s = settings();
  await drainIdle();
  await writeCommand(command);
  setStatus("基线采集中...");

  const deadline = performance.now() + Math.max(45000, s.baselineFrames * 45000);
  let started = false;
  while (performance.now() < deadline) {
    const line = await readLine(Math.max(1, deadline - performance.now()));
    if (line === null) {
      break;
    }
    const cleaned = cleanLine(line);
    if (!cleaned) {
      continue;
    }
    if (cleaned.startsWith("ERR:") || cleaned.startsWith("bad command")) {
      throw new Error(cleaned);
    }
    if (!started) {
      if (cleaned.startsWith("RECONBASE_BEGIN,")) {
        started = true;
      } else {
        continue;
      }
    }
    if (cleaned.startsWith("RECONBASE_FRAME,")) {
      const parts = cleaned.split(",");
      setStatus(`基线帧 ${parts[1]} valid=${parts[2]} invalid=${parts[3]}`);
    }
    if (cleaned.startsWith("RECONBASE_DONE,")) {
      setStatus(`基线完成 ${cleaned}`, "ok");
      return;
    }
  }
  throw new Error("等待 RECONBASE_DONE 超时");
}

async function startLoop() {
  if (state.operationInFlight || state.running) {
    return;
  }
  state.operationInFlight = true;
  state.running = true;
  state.templateNodes = null;
  updateButtons();
  try {
    while (state.running) {
      const s = settings();
      let frame;
      if (s.fastMode && state.templateNodes) {
        frame = await captureReconFastFrame(state.templateNodes);
      } else {
        frame = await captureReconFrame();
        state.templateNodes = frame.nodes.map((node) => ({ ...node }));
      }
      state.lastFrame = frame;
      drawFrame(frame);
      setFrameStats(frame);
      setStatus(`运行中 frame ${frame.frameId}`, "ok");
      await flushPendingLcdToggle();
      if (s.intervalMs > 0) {
        await sleep(s.intervalMs);
      }
    }
  } catch (error) {
    if (state.running) {
      setStatus(`采集失败: ${error.message}`, "error");
    }
  } finally {
    state.running = false;
    state.operationInFlight = false;
    updateButtons();
  }
}

function stopLoop() {
  state.running = false;
  setStatus("已停止");
  updateButtons();
}

async function captureReconFrame() {
  const command = reconCommand(false);
  await drainIdle();
  await writeCommand(command);

  const s = settings();
  const deadlineBase = Math.max(45000, s.samples * 200);
  let deadline = performance.now() + deadlineBase;
  let started = false;
  let frameId = null;
  let electrodes = s.electrodes;
  let routes = 0;
  let expectedNodes = null;
  let summary = null;
  let readingNodes = false;
  let nodes = [];
  let nodesByIndex = new Map();

  while (performance.now() < deadline) {
    const line = await readLine(Math.max(1, deadline - performance.now()));
    if (line === null) {
      break;
    }
    deadline = performance.now() + deadlineBase;
    const cleaned = cleanLine(line);
    if (!cleaned) {
      continue;
    }
    if (cleaned.startsWith("ERR:") || cleaned.startsWith("bad command")) {
      throw new Error(cleaned);
    }
    if (!started) {
      if (cleaned.startsWith("RECON_BEGIN,")) {
        started = true;
      } else {
        continue;
      }
    }
    if (cleaned.startsWith("RECON_BEGIN,")) {
      const parts = cleaned.split(",");
      started = true;
      frameId = Number(parts[1]);
      electrodes = Number(parts[2]);
      routes = Number(parts[3]);
      expectedNodes = Number(parts[4]);
      summary = null;
      readingNodes = false;
      nodes = [];
      nodesByIndex = new Map();
      continue;
    }
    if (cleaned.startsWith("RECON_SUMMARY,")) {
      summary = parseSummary(cleaned);
      continue;
    }
    if (cleaned === "node,x,y,ds") {
      readingNodes = true;
      continue;
    }
    if (cleaned === "RECON_DONE") {
      if (frameId === null || expectedNodes === null || summary === null) {
        throw new Error("RECON_DONE before complete metadata");
      }
      nodes = Array.from(nodesByIndex.values());
      if (nodes.length !== expectedNodes) {
        throw new Error(`节点数不完整: got ${nodes.length}, expected ${expectedNodes}`);
      }
      nodes.sort((a, b) => a.index - b.index);
      return { frameId, electrodes, routes, nodes, summary };
    }
    if (readingNodes) {
      const parts = cleaned.split(",");
      if (parts.length === 4) {
        const index = Number(parts[0]);
        nodesByIndex.set(index, {
          index,
          x: Number(parts[1]),
          y: Number(parts[2]),
          ds: Number(parts[3]),
        });
      }
    }
  }
  throw new Error("等待 RECON_DONE 超时");
}

async function captureReconFastFrame(templateNodes) {
  const command = reconCommand(true);
  await drainIdle();
  await writeCommand(command);

  const s = settings();
  const deadlineBase = Math.max(45000, s.samples * 200);
  let deadline = performance.now() + deadlineBase;
  let started = false;
  let frameId = null;
  let electrodes = s.electrodes;
  let routes = 0;
  let expectedNodes = null;
  let summary = null;
  let dsValues = null;

  while (performance.now() < deadline) {
    const line = await readLine(Math.max(1, deadline - performance.now()));
    if (line === null) {
      break;
    }
    deadline = performance.now() + deadlineBase;
    const cleaned = cleanLine(line);
    if (!cleaned) {
      continue;
    }
    if (cleaned.startsWith("ERR:") || cleaned.startsWith("bad command")) {
      throw new Error(cleaned);
    }
    if (!started) {
      if (cleaned.startsWith("RECONFAST_BEGIN,")) {
        started = true;
      } else {
        continue;
      }
    }
    if (cleaned.startsWith("RECONFAST_BEGIN,")) {
      const parts = cleaned.split(",");
      started = true;
      frameId = Number(parts[1]);
      electrodes = Number(parts[2]);
      routes = Number(parts[3]);
      expectedNodes = Number(parts[4]);
      summary = null;
      dsValues = null;
      continue;
    }
    if (cleaned.startsWith("RECON_SUMMARY,")) {
      summary = parseSummary(cleaned);
      continue;
    }
    if (cleaned.startsWith("RECONFAST_DS,")) {
      dsValues = cleaned.split(",").slice(1).map(Number);
      continue;
    }
    if (cleaned === "RECONFAST_DONE") {
      if (frameId === null || expectedNodes === null || summary === null || dsValues === null) {
        throw new Error("RECONFAST_DONE before complete data");
      }
      if (dsValues.length !== expectedNodes || templateNodes.length !== expectedNodes) {
        throw new Error(`reconfast 节点数不匹配: ds=${dsValues.length}, template=${templateNodes.length}`);
      }
      const nodes = templateNodes.map((node, index) => ({ ...node, ds: dsValues[index] }));
      return { frameId, electrodes, routes, nodes, summary };
    }
  }
  throw new Error("等待 RECONFAST_DONE 超时");
}

function parseSummary(line) {
  const parts = line.split(",");
  return {
    valid: Number(parts[1]),
    invalid: Number(parts[2]),
    retry: Number(parts[3]),
    dsMin: Number(parts[4]),
    dsMax: Number(parts[5]),
    dsAbsP98: Number(parts[6]),
    relL2: Number(parts[7]),
  };
}

function setFrameStats(frame) {
  ui.frameStat.textContent = String(frame.frameId);
  ui.validStat.textContent = String(frame.summary.valid);
  ui.invalidStat.textContent = String(frame.summary.invalid);
  ui.retryStat.textContent = String(frame.summary.retry);
  ui.p98Stat.textContent = formatSci(frame.summary.dsAbsP98);
  ui.relL2Stat.textContent = formatSci(frame.summary.relL2);
}

function formatSci(value) {
  return Number.isFinite(value) ? value.toExponential(3) : "-";
}

function showGesture(label, confidence, allProbas) {
  if (!ui.gestureBar || !ui.gestureName || !ui.gestureConf) return;
  ui.gestureBar.style.display = "flex";
  if (label === "unknown") {
    ui.gestureName.textContent = "?";
    ui.gestureName.style.color = "#95a5a6";
    ui.gestureConf.textContent = `(${(confidence * 100).toFixed(0)}%)`;
    ui.gestureConf.style.color = "#95a5a6";
    ui.gestureBar.style.background = "#ecf0f1";
    ui.gestureBar.style.borderColor = "#bdc3c7";
  } else {
    ui.gestureName.textContent = label;
    ui.gestureConf.textContent = `${(confidence * 100).toFixed(0)}%`;
    if (confidence >= 0.8) {
      ui.gestureName.style.color = "#27ae60";
      ui.gestureConf.style.color = "#27ae60";
      ui.gestureBar.style.background = "#d5f5e3";
      ui.gestureBar.style.borderColor = "#27ae60";
    } else if (confidence >= 0.6) {
      ui.gestureName.style.color = "#e67e22";
      ui.gestureConf.style.color = "#e67e22";
      ui.gestureBar.style.background = "#fdebd0";
      ui.gestureBar.style.borderColor = "#e67e22";
    } else {
      ui.gestureName.style.color = "#95a5a6";
      ui.gestureConf.style.color = "#95a5a6";
      ui.gestureBar.style.background = "#ecf0f1";
      ui.gestureBar.style.borderColor = "#bdc3c7";
    }
  }
}

function drawFrame(frame) {
  const ctx = ui.canvas.getContext("2d");
  const width = ui.canvas.width;
  const height = ui.canvas.height;
  const s = settings();
  const gridSize = s.gridSize;
  const image = ctx.createImageData(gridSize, gridSize);
  const values = frame.nodes.map((node) => {
    if (s.deadband > 0 && Math.abs(node.ds) < s.deadband) {
      return 0;
    }
    return node.ds;
  });

  let vmax = s.vmax > 0 ? s.vmax : percentile(values.map((v) => Math.abs(v)), 0.98);
  vmax = Math.max(s.minVmax, Number.isFinite(vmax) ? vmax : s.minVmax);

  for (let gy = 0; gy < gridSize; gy++) {
    const y = 1 - (2 * gy) / (gridSize - 1);
    for (let gx = 0; gx < gridSize; gx++) {
      const x = -1 + (2 * gx) / (gridSize - 1);
      const offset = (gy * gridSize + gx) * 4;
      if ((x * x + y * y) > 1.0) {
        image.data[offset + 0] = 248;
        image.data[offset + 1] = 250;
        image.data[offset + 2] = 252;
        image.data[offset + 3] = 255;
        continue;
      }
      const value = interpolateIdw(frame.nodes, values, x, y);
      const [r, g, b] = divergingColor(value / vmax);
      image.data[offset + 0] = r;
      image.data[offset + 1] = g;
      image.data[offset + 2] = b;
      image.data[offset + 3] = 255;
    }
  }

  const offscreen = document.createElement("canvas");
  offscreen.width = gridSize;
  offscreen.height = gridSize;
  offscreen.getContext("2d").putImageData(image, 0, 0);

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);

  const margin = 72;
  const size = Math.min(width, height) - margin * 2;
  const left = (width - size) / 2;
  const top = (height - size) / 2;
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(offscreen, left, top, size, size);

  drawBoundary(ctx, left, top, size, frame.electrodes, s.labels);
  drawColorBar(ctx, width - 55, top, 18, size, vmax);
  drawTitle(ctx, frame, left, top);
}

function interpolateIdw(nodes, values, x, y) {
  let weighted = 0;
  let weights = 0;
  for (let i = 0; i < nodes.length; i++) {
    const dx = x - nodes[i].x;
    const dy = y - nodes[i].y;
    const d2 = dx * dx + dy * dy;
    if (d2 < 1.0e-8) {
      return values[i];
    }
    const w = 1 / (d2 * d2 + 1.0e-7);
    weighted += values[i] * w;
    weights += w;
  }
  return weights > 0 ? weighted / weights : 0;
}

function divergingColor(value) {
  const t = Math.max(-1, Math.min(1, value));
  if (t >= 0) {
    return blend([246, 247, 249], [177, 38, 30], t);
  }
  return blend([246, 247, 249], [24, 91, 170], -t);
}

function blend(a, b, t) {
  const u = Math.max(0, Math.min(1, t));
  return [
    Math.round(a[0] + (b[0] - a[0]) * u),
    Math.round(a[1] + (b[1] - a[1]) * u),
    Math.round(a[2] + (b[2] - a[2]) * u),
  ];
}

function drawBoundary(ctx, left, top, size, electrodes, labels) {
  const cx = left + size / 2;
  const cy = top + size / 2;
  const radius = size / 2;
  ctx.save();
  ctx.strokeStyle = "#18212c";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.stroke();

  ctx.fillStyle = "#15181d";
  ctx.font = "15px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (let i = 0; i < electrodes; i++) {
    const angle = (Math.PI / 2) - (2 * Math.PI * i / electrodes);
    const x = cx + Math.cos(angle) * radius;
    const y = cy - Math.sin(angle) * radius;
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.fill();
    if (labels) {
      ctx.fillText(`S${i + 1}`, cx + Math.cos(angle) * (radius + 24), cy - Math.sin(angle) * (radius + 24));
    }
  }
  ctx.restore();
}

function drawColorBar(ctx, x, y, w, h, vmax) {
  const gradient = ctx.createLinearGradient(0, y + h, 0, y);
  gradient.addColorStop(0, "rgb(24, 91, 170)");
  gradient.addColorStop(0.5, "rgb(246, 247, 249)");
  gradient.addColorStop(1, "rgb(177, 38, 30)");
  ctx.save();
  ctx.fillStyle = gradient;
  ctx.fillRect(x, y, w, h);
  ctx.strokeStyle = "#4b5563";
  ctx.strokeRect(x, y, w, h);
  ctx.fillStyle = "#15181d";
  ctx.font = "13px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(formatSci(vmax), x + w + 8, y + 7);
  ctx.fillText("0", x + w + 8, y + h / 2);
  ctx.fillText(formatSci(-vmax), x + w + 8, y + h - 7);
  ctx.restore();
}

function drawTitle(ctx, frame, left, top) {
  ctx.save();
  ctx.fillStyle = "#15181d";
  ctx.font = "700 20px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  ctx.fillText(`Frame ${frame.frameId}`, left, Math.max(18, top - 44));
  ctx.fillStyle = "#626b78";
  ctx.font = "14px system-ui, sans-serif";
  ctx.fillText(
    `valid=${frame.summary.valid} invalid=${frame.summary.invalid} retry=${frame.summary.retry}`,
    left,
    Math.max(44, top - 18),
  );
  ctx.restore();
}

function percentile(values, p) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (sorted.length === 0) {
    return NaN;
  }
  if (sorted.length === 1) {
    return sorted[0];
  }
  const pos = p * (sorted.length - 1);
  const low = Math.floor(pos);
  const high = Math.min(sorted.length - 1, low + 1);
  const frac = pos - low;
  return sorted[low] + (sorted[high] - sorted[low]) * frac;
}

async function sendManualCommand() {
  if (state.operationInFlight || state.running) {
    return;
  }
  const command = ui.commandInput.value.trim();
  if (!command) {
    return;
  }
  state.operationInFlight = true;
  setBusy(true);
  try {
    await drainIdle();
    await writeCommand(command);
    setStatus(`已发送: ${command}`, "ok");
  } catch (error) {
    setStatus(`发送失败: ${error.message}`, "error");
  } finally {
    state.operationInFlight = false;
    setBusy(false);
  }
}

async function sendLcdToggleCommand() {
  await drainIdle();
  await writeCommand("lcdmode toggle");
}

async function flushPendingLcdToggle() {
  if (!state.pendingLcdToggle) {
    return;
  }

  state.pendingLcdToggle = false;
  await sendLcdToggleCommand();
  setStatus("LCD 显示已切换", "ok");
}

async function toggleLcdDisplay() {
  if (!state.connected || state.busy) {
    return;
  }

  if (state.running || state.operationInFlight) {
    state.pendingLcdToggle = true;
    setStatus("LCD 切换已排队，将在当前帧结束后发送", "ok");
    return;
  }

  state.operationInFlight = true;
  setBusy(true);
  try {
    await sendLcdToggleCommand();
    setStatus("LCD 显示已切换", "ok");
  } catch (error) {
    setStatus(`LCD 切换失败: ${error.message}`, "error");
  } finally {
    state.operationInFlight = false;
    setBusy(false);
  }
}

function drawEmpty() {
  const ctx = ui.canvas.getContext("2d");
  ctx.clearRect(0, 0, ui.canvas.width, ui.canvas.height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, ui.canvas.width, ui.canvas.height);
  ctx.fillStyle = "#626b78";
  ctx.font = "18px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText("EIT", ui.canvas.width / 2, ui.canvas.height / 2);
}

ui.connectBtn.addEventListener("click", connectSerial);
ui.disconnectBtn.addEventListener("click", disconnectSerial);
ui.initBtn.addEventListener("click", initBoard);
ui.baselineBtn.addEventListener("click", captureBaseline);
ui.startBtn.addEventListener("click", startLoop);
ui.stopBtn.addEventListener("click", stopLoop);
ui.lcdToggleBtn.addEventListener("click", toggleLcdDisplay);
ui.sendBtn.addEventListener("click", sendManualCommand);
ui.clearLogBtn.addEventListener("click", () => {
  ui.serialLog.textContent = "";
});
ui.commandInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !ui.sendBtn.disabled) {
    sendManualCommand();
  }
});

if ("serial" in navigator) {
  navigator.serial.addEventListener("disconnect", (event) => {
    if (state.port && event.target === state.port) {
      handleSerialLost(new Error("USB serial device disconnected"));
    }
  });
}

if (BRIDGE_MODE) {
  ui.connectBtn.textContent = "连接桥";
  ui.disconnectBtn.textContent = "断开桥";
  setStatus("桥接模式");
}

window.addEventListener("beforeunload", () => {
  state.running = false;
  state.readLoopAbort = true;
});

drawEmpty();
updateButtons();
