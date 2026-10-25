/**
 * 一次性品牌开场。
 *
 * 三道极细光轨从右上掠过，其中一条带暖金色尾迹，约 0.8 秒后收束。
 * 动效只改 transform 与 opacity，不引入粒子库；系统要求减少动态效果时
 * 由 CSS 直接隐藏光轨，只保留短暂淡入。
 */
export default function IntroOverlay({ exiting }: { exiting: boolean }) {
  return (
    <div
      className={`intro-overlay ${exiting ? "intro-overlay--exit" : ""}`}
      role="status"
      aria-label="正在准备 Heytalk"
    >
      <span className="intro-meteor intro-meteor--one" aria-hidden="true" />
      <span
        className="intro-meteor intro-meteor--two intro-meteor--gold"
        aria-hidden="true"
      />
      <span className="intro-meteor intro-meteor--three" aria-hidden="true" />
      <span className="sr-only">正在准备 Heytalk</span>
    </div>
  );
}
