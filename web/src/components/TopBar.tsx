import {
  NewChatIcon, PanelIcon, VolumeOffIcon, VolumeOnIcon,
} from "./icons";

interface Props {
  onNew: () => void;
  onExpand: () => void;
  soundEnabled: boolean;
  onToggleSound: () => void;
}

function SoundToggle({ soundEnabled, onToggleSound }: {
  soundEnabled: boolean;
  onToggleSound: () => void;
}) {
  return (
    <button
      id="soundToggle"
      type="button"
      onClick={onToggleSound}
      title={soundEnabled ? "关闭语音朗读" : "开启语音朗读"}
      aria-label={soundEnabled ? "关闭语音朗读" : "开启语音朗读"}
      aria-pressed={soundEnabled}
      className={`w-9 h-9 rounded-full flex items-center justify-center
                  transition-colors ${soundEnabled
                    ? "text-brand-dark bg-[#edf2f8]"
                    : "text-muted hover:bg-panel active:bg-panel-2"}`}
    >
      {soundEnabled
        ? <VolumeOnIcon className="w-[19px] h-[19px]" />
        : <VolumeOffIcon className="w-[19px] h-[19px]" />}
    </button>
  );
}

export default function TopBar({
  onNew, onExpand, soundEnabled, onToggleSound,
}: Props) {
  return (
    <header className="flex items-center justify-between px-4 pt-3 pb-1 shrink-0">
      <button
        id="toggleBtn"
        type="button"
        onClick={onExpand}
        title="展开侧边栏"
        aria-label="展开侧边栏"
        className="w-9 h-9 rounded-full text-[#5b636d] hover:bg-panel
                   active:bg-panel-2 flex items-center justify-center transition-colors"
      >
        <PanelIcon className="w-[19px] h-[19px]" />
      </button>

      <div className="flex items-center gap-1">
        <button
          id="topNewChat"
          type="button"
          onClick={onNew}
          title="开新对话"
          aria-label="开新对话"
          className="w-9 h-9 rounded-full text-[#5b636d] hover:bg-panel
                     active:bg-panel-2 flex items-center justify-center transition-colors"
        >
          <NewChatIcon className="w-[19px] h-[19px]" />
        </button>
        <SoundToggle
          soundEnabled={soundEnabled}
          onToggleSound={onToggleSound}
        />
      </div>
    </header>
  );
}

export function MobileTopBar({
  onNew, onExpand, soundEnabled, onToggleSound,
}: Props) {
  return (
    <header className="flex items-center justify-between px-4 pt-3 pb-2 shrink-0">
      <button
        id="toggleBtn"
        type="button"
        onClick={onExpand}
        title="展开侧边栏"
        aria-label="展开侧边栏"
        className="w-9 h-9 rounded-full text-[#5b636d] hover:bg-panel
                   active:bg-panel-2 flex items-center justify-center transition-colors"
      >
        <PanelIcon className="w-[19px] h-[19px]" />
      </button>

      <div className="flex items-center gap-1">
        <button
          id="topNewChat"
          type="button"
          onClick={onNew}
          title="开新对话"
          aria-label="开新对话"
          className="w-9 h-9 rounded-full text-[#5b636d] hover:bg-panel
                     active:bg-panel-2 flex items-center justify-center transition-colors"
        >
          <NewChatIcon className="w-[19px] h-[19px]" />
        </button>
        <SoundToggle
          soundEnabled={soundEnabled}
          onToggleSound={onToggleSound}
        />
      </div>
    </header>
  );
}
