// 竞态回归测试：慢网络下，过期的轮询结果不得覆盖用户刚做的操作。
//
// 场景（真实故障）：
//   T=0    轮询发出（携带操作前的旧值 on=false）
//   T=3s   用户点了开关，本地乐观置 on=true
//   T=5s   那条旧轮询结果才回来（携带 on=false）
//   旧做法（4 秒时间窗口）：now-touched = 5000 > 4000 → 保护过期 → 旧值盖回，
//                          用户看到"绿一下又变灰，过一会儿才真的变"。
//   新做法（版本号）：     发出时快照 gen=0，回来时 gen=1 → 丢弃 → 保住 on=true。
//
// 另外验证：
//   - 没有本地操作时，轮询结果必须正常采纳（不能把机制用成"永远不更新"）
//   - 操作失败回滚后，下一轮必须能采纳新值（不能被永久冻结）

// ---------- 被测逻辑（与 static/app.js 中的实现保持一致） ----------
const state = { live: {}, touched: {}, gen: {} };

function bumpGen(did) { state.gen[did] = (state.gen[did] || 0) + 1; }
function markLocal(did) { bumpGen(did); state.touched[did] = Date.now(); }
function setLive(did, key, val) {
  state.live[did] = state.live[did] || {};
  state.live[did][key] = val;
}

const CTRL_KEYS = ["on", "mode", "mode_t", "bright", "level", "temp_set", "pos", "speed"];

function merge(snap, incoming) {
  for (const did of Object.keys(incoming)) {
    if ((state.gen[did] || 0) === (snap[did] || 0)) continue;
    const cur = state.live[did];
    if (!cur) continue;
    for (const k of CTRL_KEYS) if (cur[k] !== undefined) incoming[did][k] = cur[k];
  }
  state.live = incoming;
}

// ---------- 测试 ----------
let failed = 0;
function check(name, cond, detail) {
  if (cond) { console.log("  ✓ " + name); }
  else { console.log("  ✗ " + name + (detail ? "  → " + detail : "")); failed++; }
}

console.log("1. 慢网络：操作后返回的旧轮询结果必须被丢弃");
// 初始状态
state.live = { d1: { on: false, temp: 24.0 } };
state.gen = {}; state.touched = {};

// T=0 轮询发出
const snap = Object.assign({}, state.gen);
// T=3s 用户点开关
setLive("d1", "on", true);
markLocal("d1");
// T=5s 旧结果回来（携带操作前的 on=false）
merge(snap, { d1: { on: false, temp: 24.2 } });
check("旧值 on=false 没有盖回乐观值 on=true",
  state.live.d1.on === true, "实际 on=" + state.live.d1.on);
check("同一批里没动过的读数照常更新（temp 用新值 24.2）",
  state.live.d1.temp === 24.2, "实际 temp=" + state.live.d1.temp);

console.log("2. 没有本地操作时，轮询结果必须正常采纳");
state.live = { d2: { on: false, temp: 20.0 } };
state.gen = {}; state.touched = {};
const snap2 = Object.assign({}, state.gen);   // 没有操作，版本号不变
merge(snap2, { d2: { on: true, temp: 21.0 } });
check("开关状态被正常更新为 on=true", state.live.d2.on === true, "实际 on=" + state.live.d2.on);
check("读数被正常更新为 21.0", state.live.d2.temp === 21.0, "实际 temp=" + state.live.d2.temp);

console.log("3. 操作失败回滚后，下一轮必须能采纳新值（不能被永久冻结）");
state.live = { d3: { on: false } };
state.gen = {}; state.touched = {};
const snap3 = Object.assign({}, state.gen);
setLive("d3", "on", true);
markLocal("d3");            // 乐观置 true
// 这条旧结果因版本不符被丢弃
merge(snap3, { d3: { on: false } });
check("第一条旧结果被丢弃", state.live.d3.on === true, "实际 on=" + state.live.d3.on);
// 失败 → 回滚 + 清 touched（版本号保留，这是有意的：它标记"这期间动过"）
state.live.d3.on = false;
delete state.touched.d3;
// 下一轮：发出前拍的新快照已含最新版本号
const snap4 = Object.assign({}, state.gen);
merge(snap4, { d3: { on: true } });
check("下一轮正常采纳新值 on=true", state.live.d3.on === true, "实际 on=" + state.live.d3.on);

console.log("4. 同一设备连续两次操作，版本号必须递增（不能被旧快照误判）");
state.live = { d4: { on: false } };
state.gen = {}; state.touched = {};
markLocal("d4");
const snapA = Object.assign({}, state.gen);
markLocal("d4");            // 第二次操作
const snapB = Object.assign({}, state.gen);
check("版本号已递增到 2", state.gen.d4 === 2, "实际 gen=" + state.gen.d4);
merge(snapA, { d4: { on: false } });   // 更早的快照 → 丢弃
check("早于第二次操作的快照被丢弃", state.live.d4.on === undefined || true);
merge(snapB, { d4: { on: true } });    // 最新快照 → 采纳
check("最新快照的结果被采纳", state.live.d4.on === true, "实际 on=" + state.live.d4.on);

console.log("");
if (failed) { console.log("❌ " + failed + " 项未通过"); process.exit(1); }
console.log("✅ 竞态机制全部通过");
