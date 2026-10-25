/**
 * src/components/Login.tsx —— 登录页（**只有托管模式才会出现**）
 *
 * 为什么本机模式不显示它：单人自己用不该被登录挡住。
 * 后端 `bootstrap.auth_required` 为 false 时（没配置任何用户），
 * 这个组件根本不会被渲染。见 src/server/auth.py 的说明。
 *
 * 为什么没有"注册 / 忘记密码"：账号是主人在服务器上用
 * `python scripts/set_user.py add` 开的，一共两三个人。
 * 加注册流程反而会带来"谁都能注册"的问题。
 */
import { useState } from "react";
import { BrandMark } from "./icons";

interface Props {
  onLogin: (userId: string, password: string) => Promise<void>;
  /** 用邀请码开通（成功后会自动登录） */
  onRedeem?: (code: string, userId: string, password: string) => Promise<void>;
  /** 登录失败的中文提示 */
  error?: string | null;
}

export default function Login({ onLogin, onRedeem, error }: Props) {
  // 'login' | 'redeem' —— 两种模式共用一个页面，
  // 因为对用户来说都是"进门"，分成两个页面反而多一次跳转。
  const [mode, setMode] = useState<"login" | "redeem">("login");
  const [userId, setUserId] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    // ★ 字段都去掉首尾空白：口令/邀请码常是从聊天里复制来的，
    //   末尾带着空格或换行而用户看不出来，表现为"一模一样却进不去"。
    const uid = userId.trim();
    const pw = password.trim();
    if (!uid || !pw || busy) return;
    if (mode === "redeem" && !code.trim()) return;
    setBusy(true);
    try {
      if (mode === "redeem") {
        await onRedeem?.(code.trim(), uid, pw);
      } else {
        await onLogin(uid, pw);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="h-full flex items-center justify-center bg-white px-5">
      <form onSubmit={submit} className="w-full max-w-[340px]">
        <div className="flex items-center gap-2 mb-7 justify-center">
          <BrandMark className="w-8 h-8 block shrink-0" />
          <span className="text-[20px] font-extrabold tracking-[-0.5px] text-ink leading-none">
            Heytalk
          </span>
        </div>

        {/* ---- 邀请码输入框：只在"用邀请码开通"模式下出现 ---- */}
        {mode === "redeem" && (
          <>
            <label className="block text-[13px] text-muted mb-1.5">邀请码</label>
            <input
              id="redeemCode"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="heytalk-xxxxxxxx"
              autoFocus
              className="w-full rounded-xl border border-line px-3.5 py-2.5 mb-4
                         text-[15px] outline-none focus:border-brand/50
                         transition-colors font-mono"
            />
          </>
        )}

        <label className="block text-[13px] text-muted mb-1.5">
          {mode === "redeem"
            ? "起一个用户名（英文/数字，登录时用它）"
            : "用户名（创建时给的 id，不是显示名）"}
        </label>
        <input
          id="loginUser"
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          autoFocus={mode === "login"}
          autoComplete="username"
          className="w-full rounded-xl border border-line px-3.5 py-2.5 mb-1
                     text-[15px] outline-none focus:border-brand/50 transition-colors"
        />
        <label className={`block text-[13px] text-muted mb-1.5 ${mode === "redeem" ? "mt-3" : ""}`}>
          {mode === "redeem" ? "自己设一个口令（至少 6 位）" : "口令"}
        </label>
        <input
          id="loginPass"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={mode === "redeem" ? "new-password" : "current-password"}
          className="w-full rounded-xl border border-line px-3.5 py-2.5
                     text-[15px] outline-none focus:border-brand/50 transition-colors"
        />
        {/* 从聊天/文档里复制口令时，末尾常带上空格或换行，而用户看不出差别，
            表现就是"明明一模一样却登不进去"。提交时会去掉首尾空白（服务端也会去），
            这里顺带提示一句，避免用户怀疑是自己记错了。 */}

        {error && (
          <div className="mt-3">
            <p className="text-[13px] text-[#c0392b]">{error}</p>
            {/* 把最常见的原因直接写出来。主人实际卡在这里：
                他用 `--name 我` 建账号，于是拿"我"和"admin"去登录，
                而登录要用的是 id（如 me）。报错只说"用户名或口令不对"
                会让人以为是口令错，白试很多次。 */}
            {mode === "login" && (
              <p className="text-[12px] text-muted mt-1.5 leading-[1.6]">
                提示：用户名是创建账号时命令行里的那个 id（例如
                <code className="bg-panel px-1 rounded">me</code>），
                <b>不是</b> <code className="bg-panel px-1 rounded">--name</code> 写的显示名。
                口令首尾的空格会被自动忽略。
              </p>
            )}
          </div>
        )}

        <button
          id="loginSubmit"
          type="submit"
          disabled={busy || !userId.trim() || !password}
          className="w-full mt-6 rounded-xl bg-brand text-white text-[15px] py-2.5
                     hover:bg-brand-dark disabled:opacity-40 transition-colors"
        >
          {busy
            ? (mode === "redeem" ? "开通中…" : "登录中…")
            : (mode === "redeem" ? "开通并进入" : "登录")}
        </button>

        {/* 模式切换。只在服务端支持邀请码时才显示（onRedeem 存在）。
            用一行文字链接而不是两个按钮：这不是并列的主操作，
            默认路径永远是"登录"。 */}
        {onRedeem && (
          <p className="text-[12px] text-muted mt-4 text-center">
            {mode === "login" ? (
              <>
                有邀请码？
                <button
                  id="toRedeem"
                  type="button"
                  onClick={() => { setMode("redeem"); }}
                  className="text-brand-dark hover:underline ml-1"
                >
                  开通账号
                </button>
              </>
            ) : (
              <>
                已经有账号？
                <button
                  id="toLogin"
                  type="button"
                  onClick={() => { setMode("login"); }}
                  className="text-brand-dark hover:underline ml-1"
                >
                  去登录
                </button>
              </>
            )}
          </p>
        )}

        {mode === "redeem" && (
          <p className="text-[12px] text-muted mt-3 leading-[1.7] text-center">
            邀请码由服务提供者发给你，一次性使用。
            <br />
            口令由你自己设置，<b>服务提供者不知道它</b>。
          </p>
        )}
      </form>
    </div>
  );
}
