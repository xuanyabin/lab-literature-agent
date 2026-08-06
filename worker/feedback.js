/**
 * 反馈 Worker，三个端点：
 *
 * 1) 星标一键反馈：GET /fb?u=<邮箱>&p=<论文id>&v=<1-5>&s=<签名>
 * 校验 HMAC 签名后用 GitHub API 把反馈 YAML 提交到仓库 main 的
 * feedback_data/pending/；每日流水线 checkout 时随 main 带回，
 * 由 python -m feedback 的学习闭环直接消费（与 IMAP 收集的反馈同一队列）。
 *
 * 2) 网页版新增关键词：GET /kw?u=<邮箱>&d=<日期>&k=<关键词>&s=<签名>
 * 校验签名后把关键词 YAML 提交到 feedback_data/keywords/pending/（独立目录，
 * 与星标 pending 队列互不干扰），由 python -m feedback 的
 * feedback/collector.py collect_keyword_queue 消费：清洗去重后经
 * processing/term_expander.add_feedback_terms 追加到该用户自动词表的
 * feedback_added，次日检索即生效（与邮件 "+关键词" 行同一落地通道）。
 * 文件名 kw_u<sha256(邮箱) 前 8 位>_<sha256(关键词) 前 12 位>.yaml——同一
 * （用户, 关键词）重复提交幂等；关键词本身只进文件内容不进文件名。
 * 签名 msg 为 "<邮箱>|<日期>"，不覆盖关键词内容（关键词是用户运行时输入，
 * 页面生成时无法预知；与星标同理，签名 URL 本身即凭证）——与
 * mailer/digest_builder.py 的 _webhook_keyword_url 必须严格一致，改动需两侧同步。
 *
 * 3) 网页版"用文献优化关键词"：GET /sp?u=<邮箱>&d=<日期>&sp=<URL编码的文献列表>&s=<签名>
 * 镜像 /kw：用户粘贴自己领域感兴趣的文献（DOI 或 PMID，每行一个，一次 ≤10 篇，
 * 前端已逐行校验格式），提交到 feedback_data/seed_papers/pending/，由
 * python -m feedback 的 feedback/collector.py collect_seed_papers_queue 消费：
 * 逐条抓标题摘要 → LLM 提炼检索词 → add_feedback_terms 落 feedback_added。
 * 文件名 sp_u<sha256(邮箱) 前 8 位>_<sha256(文献列表) 前 12 位>.yaml——同一
 * （用户, 文献列表）重复提交幂等；文献列表只进文件内容不进文件名。
 * 签名 msg 同为 "<邮箱>|<日期>"（与 /kw 同构），与 mailer/digest_builder.py 的
 * _webhook_seed_papers_url 必须严格一致，改动需两侧同步。
 * YAML 字段 user_email / papers(多行原文，JSON.stringify 双引号包裹，\n 转义，
 * Python 侧 yaml.safe_load 还原) / date / source("seed_papers_webhook") /
 * timestamp——与 feedback/collector.py collect_seed_papers_queue 逐字段对应，
 * 改动需两侧同步。
 *
 * 响应两种模式（签名校验与幂等逻辑两模式完全一致）：
 *   - HTML 确认页（默认）：邮件里星标链接点开看到的中文结果页（向后兼容旧邮件）；
 *   - JSON（查询参数 format=json，或请求头 Accept: application/json）：
 *     网页版完整报告（GitHub Pages）卡片内五星按钮的页内 fetch 使用，
 *     形如 { ok, status, message, detail }，成功时另带 value / result。
 * 所有响应带 CORS 头（Access-Control-Allow-Origin: *；端点安全由 HMAC 签名
 * 保证，星标 URL 本身即凭证），OPTIONS 预检返回 204。网页版 fetch 只带
 * safelisted 的 Accept 头，实际不会触发预检。
 *
 * 环境变量（Worker Settings → Variables）：
 *   GITHUB_TOKEN    fine-grained PAT，仅授权本仓库 Contents: Read and write（Secret 类型）
 *   GITHUB_REPO     owner/name
 *   FEEDBACK_SECRET HMAC 签名密钥，与仓库 Repository Secret 同名同值（Secret 类型）
 *   GITHUB_BRANCH   可选，默认 main
 *
 * 签名约定（与 mailer/digest_builder.py 的 _webhook_star_url 必须严格一致，改动需两侧同步）：
 *   HMAC-SHA256(key=FEEDBACK_SECRET utf-8, msg="<user_email>|<paper_id>|<value>")，
 *   取 hex 前 16 位。
 * 文件名与 YAML 字段约定（与 feedback/store.py 的 save_pending 逐行对应）：
 *   文件名 u<sha256(user_email utf-8) 前 8 位>_p<paper_id>_v<value>.yaml——同名即同一条
 *   反馈，文件内容相同则跳过提交（幂等）；字段 user_email / paper_id / value(字符串) /
 *   reason / source / timestamp(UTC ISO)，字符串值用 JSON.stringify 双引号包裹（合法
 *   YAML 标量，yaml.safe_load 解析结果与 Python 侧一致），source 固定 "star_webhook"。
 *
 * 注意：本文件改动后需重新部署 Worker（wrangler deploy / Dashboard 部署）才生效。
 */

const GITHUB_API = "https://api.github.com";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {  // CORS 预检（网页版 fetch 实际用不到，兜底）
      return new Response(null, { status: 204, headers: corsHeaders() });
    }
    const json = wantsJson(request, url);
    if (url.pathname === "/kw") {
      return handleKeyword(env, url, json);
    }
    if (url.pathname === "/sp") {
      return handleSeedPapers(env, url, json);
    }
    if (url.pathname !== "/fb") {
      return respond(404, json, "页面不存在");
    }
    const u = url.searchParams.get("u") || "";
    const p = url.searchParams.get("p") || "";
    const v = url.searchParams.get("v") || "";
    const s = (url.searchParams.get("s") || "").toLowerCase();

    // 参数白名单校验（p 进文件名，论文 id 即 SQLite 自增 id，只允许数字）
    if (!u.includes("@") || !/^\d+$/.test(p) || !/^[1-5]$/.test(v) ||
        !/^[0-9a-f]{16}$/.test(s)) {
      return respond(400, json, "链接参数无效", "请从每日推荐邮件或网页版报告中点击星标。");
    }
    if (!env.GITHUB_TOKEN || !env.GITHUB_REPO || !env.FEEDBACK_SECRET) {
      return respond(500, json, "服务未配置完成", "Worker 缺少环境变量，请联系管理员。");
    }
    const expected = await hmacHex16(env.FEEDBACK_SECRET, `${u}|${p}|${v}`);
    if (!timingSafeEqual(expected, s)) {
      return respond(403, json, "签名无效", "该链接未通过校验，请从每日推荐邮件中重新点击星标。");
    }

    // 文件名 / YAML 字段与 feedback/store.py save_pending 一一对应（见文件头注释）
    const name = `u${(await sha256Hex(u)).slice(0, 8)}_p${p}_v${v}.yaml`;
    const yaml = [
      `user_email: ${JSON.stringify(u)}`,
      `paper_id: ${p}`,
      `value: ${JSON.stringify(v)}`,
      `reason: ""`,
      `source: "star_webhook"`,
      `timestamp: ${JSON.stringify(new Date().toISOString())}`,
    ].join("\n") + "\n";

    const branch = env.GITHUB_BRANCH || "main";
    try {
      const result = await commitToGitHub(
        env, `feedback_data/pending/${name}`, yaml, branch, `feedback: p${p} v${v}`);
      const detail = result === "skipped"
        ? "相同反馈已存在，无需重复提交。"
        : "感谢反馈，推荐会越推越准。";
      return respond(200, json, `⭐${v} 反馈已记录，可关闭本页`, detail,
                     { value: Number(v), result });
    } catch (err) {
      console.error("GitHub 提交失败：", err);
      return respond(502, json, "记录失败，请稍后重试", "反馈服务暂时不可用，稍后再点一次星标即可。");
    }
  },
};

// /kw 新增关键词（网页版报告页内 fetch）：签名 msg "<邮箱>|<日期>"（见文件头注释），
// 落盘 feedback_data/keywords/pending/，由 collect_keyword_queue 消费进自动词表
async function handleKeyword(env, url, json) {
  const u = url.searchParams.get("u") || "";
  const d = url.searchParams.get("d") || "";
  const k = (url.searchParams.get("k") || "").trim();
  const s = (url.searchParams.get("s") || "").toLowerCase();

  // 参数白名单校验（k 只进文件内容不进文件名，限长防滥用；清洗/去重在 Python 消费侧）
  if (!u.includes("@") || !/^\d{4}-\d{2}-\d{2}$/.test(d) || !k || k.length > 200 ||
      !/^[0-9a-f]{16}$/.test(s)) {
    return respond(400, json, "链接参数无效", "请从当日网页版报告底部的新增关键词入口提交。");
  }
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO || !env.FEEDBACK_SECRET) {
    return respond(500, json, "服务未配置完成", "Worker 缺少环境变量，请联系管理员。");
  }
  const expected = await hmacHex16(env.FEEDBACK_SECRET, `${u}|${d}`);
  if (!timingSafeEqual(expected, s)) {
    return respond(403, json, "签名无效", "该链接未通过校验，请从当日网页版报告重新提交。");
  }

  // 文件名只含哈希（关键词可能含任意字符）；字段约定见文件头注释
  const name = `kw_u${(await sha256Hex(u)).slice(0, 8)}_${(await sha256Hex(k)).slice(0, 12)}.yaml`;
  const yaml = [
    `user_email: ${JSON.stringify(u)}`,
    `keyword: ${JSON.stringify(k)}`,
    `date: ${JSON.stringify(d)}`,
    `source: "keyword_webhook"`,
    `timestamp: ${JSON.stringify(new Date().toISOString())}`,
  ].join("\n") + "\n";

  const branch = env.GITHUB_BRANCH || "main";
  try {
    const result = await commitToGitHub(
      env, `feedback_data/keywords/pending/${name}`, yaml, branch, "feedback: 新增关键词");
    const detail = result === "skipped"
      ? "相同关键词已提交过，无需重复提交。"
      : "次日检索即生效，可随时继续补充。";
    return respond(200, json, "关键词已提交，次日检索生效", detail,
                   { keyword: k, result });
  } catch (err) {
    console.error("GitHub 提交失败：", err);
    return respond(502, json, "提交失败，请稍后重试", "反馈服务暂时不可用，稍后再提交一次即可。");
  }
}

// /sp 文献输入优化关键词（网页版报告页内 fetch）：签名 msg "<邮箱>|<日期>" 与 /kw 同构
// （见文件头注释），落盘 feedback_data/seed_papers/pending/，由
// collect_seed_papers_queue 抓取文献并提炼检索词进自动词表
async function handleSeedPapers(env, url, json) {
  const u = url.searchParams.get("u") || "";
  const d = url.searchParams.get("d") || "";
  const sp = (url.searchParams.get("sp") || "").trim();
  const s = (url.searchParams.get("s") || "").toLowerCase();

  // 参数白名单校验（sp 只进文件内容不进文件名，限长防滥用；逐条解析在 Python 消费侧）
  if (!u.includes("@") || !/^\d{4}-\d{2}-\d{2}$/.test(d) || !sp || sp.length > 2000 ||
      !/^[0-9a-f]{16}$/.test(s)) {
    return respond(400, json, "链接参数无效", "请从当日网页版报告的文献输入入口提交。");
  }
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO || !env.FEEDBACK_SECRET) {
    return respond(500, json, "服务未配置完成", "Worker 缺少环境变量，请联系管理员。");
  }
  const expected = await hmacHex16(env.FEEDBACK_SECRET, `${u}|${d}`);
  if (!timingSafeEqual(expected, s)) {
    return respond(403, json, "签名无效", "该链接未通过校验，请从当日网页版报告重新提交。");
  }

  // 文件名只含哈希（文献列表可能含任意字符）；字段约定见文件头注释
  const name = `sp_u${(await sha256Hex(u)).slice(0, 8)}_${(await sha256Hex(sp)).slice(0, 12)}.yaml`;
  const yaml = [
    `user_email: ${JSON.stringify(u)}`,
    `papers: ${JSON.stringify(sp)}`,
    `date: ${JSON.stringify(d)}`,
    `source: "seed_papers_webhook"`,
    `timestamp: ${JSON.stringify(new Date().toISOString())}`,
  ].join("\n") + "\n";

  const branch = env.GITHUB_BRANCH || "main";
  try {
    const result = await commitToGitHub(
      env, `feedback_data/seed_papers/pending/${name}`, yaml, branch,
      "feedback: 文献输入优化关键词");
    const detail = result === "skipped"
      ? "相同文献列表已提交过，无需重复提交。"
      : "提炼出的新检索词次日生效。";
    return respond(200, json, "文献已提交，明日生效", detail, { result });
  } catch (err) {
    console.error("GitHub 提交失败：", err);
    return respond(502, json, "提交失败，请稍后重试", "反馈服务暂时不可用，稍后再提交一次即可。");
  }
}

// 网页版报告（GitHub Pages）页内 fetch 跨域调用所需；签名即凭证，放开 Origin 无额外风险
function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Accept, Content-Type",
  };
}

// JSON 模式：format=json 查询参数或 Accept: application/json 请求头（网页版 fetch 两者都带）
function wantsJson(request, url) {
  return (url.searchParams.get("format") || "").toLowerCase() === "json" ||
    (request.headers.get("Accept") || "").includes("application/json");
}

// json=true 返回 JSON（网页版页内 fetch）；否则返回原中文 HTML 确认页（邮件链接向后兼容）
function respond(status, json, title, detail = "", extra = {}) {
  if (json) {
    return new Response(
      JSON.stringify({ ok: status < 400, status, message: title, detail, ...extra }),
      { status, headers: { "Content-Type": "application/json; charset=utf-8", ...corsHeaders() } });
  }
  return page(status, title, detail);
}

// HMAC-SHA256 hex 前 16 位，对应 mailer/digest_builder.py _webhook_star_url
async function hmacHex16(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return toHex(sig).slice(0, 16);
}

// sha256 hex，对应 feedback/store.py _filename 的哈希输入（user_email 原样 utf-8，不做归一化）
async function sha256Hex(text) {
  return toHex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

function toHex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function base64Utf8(text) {
  // btoa 只接受 Latin-1：先 UTF-8 编码为字节再逐字节转换（反馈文件极小，无性能问题）
  let binary = "";
  for (const b of new TextEncoder().encode(text)) binary += String.fromCharCode(b);
  return btoa(binary);
}

async function githubRequest(env, apiPath, options = {}) {
  return fetch(`${GITHUB_API}${apiPath}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "star-feedback-worker",  // GitHub API 要求必须带 User-Agent
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
}

// 同路径文件：不存在则新建，内容相同则跳过（幂等），不同则带 sha 覆盖（如时间戳更新）
async function commitToGitHub(env, path, content, branch, message) {
  const encoded = base64Utf8(content);
  const getResp = await githubRequest(
    env, `/repos/${env.GITHUB_REPO}/contents/${path}?ref=${encodeURIComponent(branch)}`);
  let sha;
  if (getResp.status === 200) {
    const existing = await getResp.json();
    // GitHub 返回的 base64 每 60 字符换行，去空白后比对
    if ((existing.content || "").replace(/\s/g, "") === encoded) return "skipped";
    sha = existing.sha;
  } else if (getResp.status !== 404) {
    throw new Error(`GET ${path} -> ${getResp.status}`);
  }
  const body = { message, content: encoded, branch };
  if (sha) body.sha = sha;
  const putResp = await githubRequest(
    env, `/repos/${env.GITHUB_REPO}/contents/${path}`,
    { method: "PUT", body: JSON.stringify(body) });
  if (!putResp.ok) throw new Error(`PUT ${path} -> ${putResp.status}`);
  return sha ? "updated" : "created";
}

// 极简中文结果页（title/detail 均为代码内常量，不含用户输入，无需转义）
function page(status, title, detail = "") {
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; background: #f6f7f9; color: #333; }
  .box { background: #fff; border-radius: 12px; padding: 40px 48px; text-align: center;
         box-shadow: 0 2px 12px rgba(0,0,0,.08); }
  h1 { font-size: 22px; margin: 0 0 12px; }
  p { color: #777; margin: 0; font-size: 14px; }
</style>
</head>
<body><div class="box"><h1>${title}</h1>${detail ? `<p>${detail}</p>` : ""}</div></body>
</html>`;
  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", ...corsHeaders() },
  });
}
