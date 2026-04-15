import { useState } from "react";
import PlaygroundView from "./views/PlaygroundView";
import ContractUploadView from "./views/ContractUploadView";

type Tab = "playground" | "contract";

export default function App() {
  const [tab, setTab] = useState<Tab>("playground");

  return (
    <div className="min-h-full flex flex-col">
      <header className="border-b border-line/60 backdrop-blur sticky top-0 z-10 bg-ink/80">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-3 sm:py-4 flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 rounded-lg bg-gradient-to-br from-emerald-400 to-blue-500 grid place-items-center text-slate-900 font-black">
              T
            </div>
            <div>
              <div className="font-semibold leading-tight">TrustLayer</div>
              <div className="text-xs text-slate-400 leading-tight hidden sm:block">
                LLM output integrity checker
              </div>
            </div>
          </div>

          <nav className="flex items-center gap-1 bg-surface border border-line rounded-lg p-1 text-sm">
            <TabButton
              active={tab === "playground"}
              onClick={() => setTab("playground")}
              label="Playground"
              hint="Generic verifier"
            />
            <TabButton
              active={tab === "contract"}
              onClick={() => setTab("contract")}
              label="Contract Reviewer"
              hint="Demo app"
            />
          </nav>

          <a
            href="http://localhost:8000/docs"
            target="_blank"
            rel="noreferrer"
            className="btn-ghost hidden sm:inline-flex"
          >
            API docs ↗
          </a>
        </div>
      </header>

      <main className="flex-1">
        {tab === "playground" ? <PlaygroundView /> : <ContractUploadView />}
      </main>

      <footer className="border-t border-line/60 text-xs text-slate-500">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <span>
            TrustLayer · engine verifies every claim against your source.
          </span>
          <span className="font-mono">v0.3.0</span>
        </div>
      </footer>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  label,
  hint,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  hint: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-3 py-1.5 rounded-md transition-colors text-left ${
        active
          ? "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30"
          : "text-slate-300 hover:text-slate-100 border border-transparent"
      }`}
      title={hint}
    >
      {label}
    </button>
  );
}
