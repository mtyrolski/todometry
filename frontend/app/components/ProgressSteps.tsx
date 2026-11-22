"use client";

type ProgressStep = {
  label: string;
  hint: string;
};

type DashboardProgressLane = {
  id: string;
  label: string;
  detail: string;
  current: number;
  total: number;
  unit: string | null;
  status: "queued" | "active" | "done";
  updatedAt: string;
};

export type DashboardProgress = {
  active: boolean;
  stage: string | null;
  step: number;
  totalSteps: number;
  startedAt: string | null;
  updatedAt: string | null;
  detail: string | null;
  subCurrent?: number | null;
  subTotal?: number | null;
  lanes?: DashboardProgressLane[];
  error: string | null;
};

const STEPS: ProgressStep[] = [
  {
    label: "Checking Todoist updates",
    hint: "Refreshing projects, tasks, and activity when needed"
  },
  {
    label: "Resolving project hierarchy",
    hint: "Resolving roots across active and archived projects"
  },
  {
    label: "Preparing dashboard data",
    hint: "Loading metadata and caches"
  }
];

const FALLBACK_TITLE = "Refreshing dashboard data";

export function ProgressSteps({ progress }: { progress: DashboardProgress | null }) {
  if (!progress?.active) return null;

  const totalSteps = STEPS.length;
  const stepIndex = progress.step > 0 ? Math.min(progress.step - 1, totalSteps - 1) : 0;
  const ratio = (stepIndex + 1) / totalSteps;
  const activeStep = STEPS[stepIndex];
  const stageLabel = progress.stage ?? activeStep?.label ?? "Working";
  const detail = progress.detail ?? activeStep?.hint ?? "";
  const subTotal = progress.subTotal ?? 0;
  const subCurrent = progress.subCurrent ?? 0;
  const subRatio = subTotal > 0 ? Math.min(1, subCurrent / subTotal) : null;
  const subPercent = subRatio !== null ? Math.round(subRatio * 100) : null;
  const lanes = progress.lanes ?? [];

  return (
    <section className="progressCard" role="status" aria-live="polite">
      <div className="progressHeader">
        <div>
          <p className="progressTitle">{FALLBACK_TITLE}</p>
          <p className="progressStage">{stageLabel}</p>
          {detail ? <p className="progressDetail">{detail}</p> : null}
          {subRatio !== null ? (
            <div className="progressSub">
              <div className="progressSubMeta">
                {subPercent}% • {subCurrent}/{subTotal}
              </div>
              <div className="progressSubTrack" aria-hidden>
                <div className="progressSubFill" style={{ width: `${subPercent}%` }} />
              </div>
            </div>
          ) : null}
          {progress.error ? <p className="progressError">{progress.error}</p> : null}
        </div>
        <p className="progressMeta">
          Step {stepIndex + 1} of {totalSteps}
        </p>
      </div>

      {lanes.length > 0 ? (
        <div className="progressLanes">
          {lanes.map((lane) => {
            const laneRatio = lane.total > 0 ? Math.min(1, lane.current / lane.total) : 0;
            const lanePercent = Math.round(laneRatio * 100);
            const laneUnit = lane.unit ? ` ${lane.unit}` : "";
            const laneMeta = lane.status === "queued"
              ? "Queued"
              : `${lanePercent}% • ${lane.current}/${lane.total}${laneUnit}`;
            return (
              <div
                key={lane.id}
                className={`progressLane progressLane-${lane.status}`}
                role="progressbar"
                aria-label={lane.label}
                aria-valuemin={0}
                aria-valuemax={lane.total}
                aria-valuenow={lane.current}
              >
                <div className="progressLaneHeader">
                  <span className="progressLaneLabel">{lane.label}</span>
                  <span className="progressLaneMeta">{laneMeta}</span>
                </div>
                <p className="progressLaneDetail">{lane.detail}</p>
                <div className="progressLaneTrack" aria-hidden>
                  <div
                    className="progressLaneFill"
                    style={{ width: `${lanePercent}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      <div className="progressTrack" aria-hidden>
        <div className="progressFill" style={{ width: `${Math.round(ratio * 100)}%` }} />
        <div className="progressSweep" />
      </div>

      <div className="progressSteps" aria-hidden>
        {STEPS.map((step, idx) => {
          const state = idx < stepIndex ? "done" : idx === stepIndex ? "active" : "pending";
          return (
            <div key={step.label} className={`progressStep progressStep-${state}`}>
              <span className="progressDot" />
              <div>
                <p className="progressLabel">{step.label}</p>
                <p className="progressHint">{step.hint}</p>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
