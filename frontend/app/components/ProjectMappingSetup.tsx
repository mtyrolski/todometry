"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { normalizeSearch } from "../lib/mobile";

export type MappingProject = { id: string; name: string; path: string; archived: boolean };
export type ProjectRule = { targetId: string; targetName: string };
export type ProjectRulePatch = Record<string, ProjectRule | null>;

type Step = "main" | { projectId: string | null };
type SaveStatus = "idle" | "saving" | { error: string };

export function ProjectMappingSetup({ projects, rules, onSave, onClose, cancelLabel = "Cancel", busy = false }: {
  projects: MappingProject[];
  rules: Record<string, ProjectRule>;
  onSave: (patch: ProjectRulePatch) => Promise<void>;
  onClose: () => void;
  cancelLabel?: string;
  busy?: boolean;
}) {
  const ordered = useMemo(() => {
    const named = projects.map((project) => ({
      ...project,
      label: project.path + (project.archived ? " · archived" : ""),
    }));
    const counts = new Map<string, number>();
    for (const { label } of named) counts.set(label, (counts.get(label) ?? 0) + 1);
    return named.map((project) => ({
      ...project,
      label: counts.get(project.label) === 1 ? project.label : project.label + " · ID " + project.id,
    })).sort((left, right) => left.label.localeCompare(right.label));
  }, [projects]);
  const savedRules = useMemo(() => new Map(Object.entries(rules)), [rules]);
  // A self-target selects a main project, null removes a rule, and an absent entry keeps it.
  const [draft, setDraft] = useState(() => new Map<string, string | null>());
  const [step, setStep] = useState<Step>("main");
  const [search, setSearch] = useState("");
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");
  const heading = useRef<HTMLHeadingElement>(null);

  const chosenTarget = (id: string) => {
    const choice = draft.get(id);
    return choice === undefined ? savedRules.get(id)?.targetId : choice;
  };
  const isMain = (id: string) => chosenTarget(id) === id;
  const main = ordered.filter(({ id }) => isMain(id));
  const remaining = ordered.filter(({ id }) => {
    if (isMain(id)) return false;
    const saved = savedRules.get(id);
    return main.length > 0 || Boolean(saved && saved.targetId !== id);
  });
  const index = step === "main" ? -1 : remaining.findIndex(({ id }) => id === step.projectId);
  const project = remaining[index];
  const previous = remaining[(project ? index : remaining.length) - 1];
  const savedRule = project ? savedRules.get(project.id) : undefined;
  const chosen = project ? chosenTarget(project.id) : undefined;
  const target = main.find(({ id }) => id === chosen);
  const savedTarget = !target && chosen && chosen === savedRule?.targetId
    ? ordered.find(({ id }) => id === chosen)?.label ?? savedRule.targetName
    : "";
  const filter = normalizeSearch(search);
  const visibleProjects = filter ? ordered.filter(({ label }) => normalizeSearch(label).includes(filter)) : ordered;
  const saving = saveStatus === "saving";
  const disabled = busy || saving;

  useEffect(() => { heading.current?.focus(); }, [step]);

  const setChoice = (id: string, targetId: string | null | undefined) => {
    setDraft((current) => {
      const next = new Map(current);
      if (targetId === undefined) next.delete(id);
      else next.set(id, targetId);
      return next;
    });
  };
  const leaveUnselected = (id: string) => setChoice(id, savedRules.get(id)?.targetId === id ? null : undefined);
  const showProject = (position: number) => setStep({ projectId: remaining[position]?.id ?? null });

  const save = async () => {
    if (disabled) return;
    const entries: [string, ProjectRule | null][] = [];
    const mainById = new Map(main.map((item) => [item.id, item]));
    for (const { id } of ordered) {
      const saved = savedRules.get(id);
      const choice = draft.get(id);
      const group = mainById.get(choice ?? id);
      if (group) {
        if (saved?.targetId !== group.id || saved.targetName !== group.name) {
          entries.push([id, { targetId: group.id, targetName: group.name }]);
        }
      } else if (saved && (choice === null || saved.targetId === id)) {
        entries.push([id, null]);
      }
    }
    if (!entries.length) {
      onClose();
      return;
    }
    setSaveStatus("saving");
    try {
      await onSave(Object.fromEntries(entries));
    } catch (cause) {
      setSaveStatus({ error: cause instanceof Error ? cause.message : "Could not save project mapping." });
      return;
    }
    setSaveStatus("idle");
    onClose();
  };

  return (
    <section className="card mappingSetup" aria-label="Project mapping setup" aria-busy={saving}>
      {step === "main" ? (
        <>
          <h2 ref={heading} tabIndex={-1}>Choose main projects</h2>
          <p className="muted">Pick the groups you want in your dashboard. Then assign other projects to those groups or skip them.</p>
          <label className="control">Find a main project
            <input className="textInput" type="search" value={search} disabled={disabled} onChange={(event) => setSearch(event.target.value)} />
          </label>
          <div className="mappingChoices">
            {visibleProjects.map((item) => (
              <label key={item.id} className="settingsToggle">
                <input type="checkbox" checked={isMain(item.id)} disabled={disabled} onChange={(event) => {
                  if (event.target.checked) setChoice(item.id, item.id);
                  else leaveUnselected(item.id);
                }} />
                {item.label}
              </label>
            ))}
            {!visibleProjects.length && (
              <p className="muted">{ordered.length ? "No matching projects." : "No projects available yet."}</p>
            )}
          </div>
          <p className="muted tiny" role="status">{main.length} selected</p>
          <div className="mappingActions">
            <button type="button" className="button buttonSmall" disabled={disabled || (!main.length && !ordered.some(({ id }) => savedRules.has(id)))} onClick={() => showProject(0)}>Continue</button>
            <button type="button" className="button buttonSmall buttonGhost" disabled={saving} onClick={onClose}>{cancelLabel}</button>
          </div>
        </>
      ) : (
        <>
          <h2 ref={heading} tabIndex={-1}>{project ? "Assign projects" : "Review project mapping"}</h2>
          {project ? (
            <>
              <p className="muted tiny">Project {index + 1} of {remaining.length}</p>
              <h3>{project.label}</h3>
              <label className="control">Group under
                <select className="select" value={target?.id ?? ""} onChange={(event) => setChoice(project.id, event.target.value)} disabled={disabled || !main.length}>
                  <option value="" disabled>Select a main project</option>
                  {main.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                </select>
              </label>
              {savedTarget && (
                <p className="muted tiny">Saved group · {savedTarget}</p>
              )}
              <div className="mappingActions">
                <button type="button" className="button buttonSmall" disabled={!target || disabled} onClick={() => showProject(index + 1)}>Assign and next</button>
                <button type="button" className="button buttonSmall buttonGhost" disabled={disabled} onClick={() => {
                  leaveUnselected(project.id);
                  showProject(index + 1);
                }}>Skip project</button>
                {savedRule && <button type="button" className="button buttonSmall buttonGhost" disabled={disabled} onClick={() => {
                  setChoice(project.id, null);
                  showProject(index + 1);
                }}>Remove individual rule</button>}
              </div>
            </>
          ) : (
            <p className="muted">Save your selections to apply them to the dashboard.</p>
          )}
          {remaining.length > 1 && (
            <label className="control">Go to project
              <select className="select" value={project?.id ?? ""} disabled={disabled} onChange={(event) => setStep({ projectId: event.target.value })}>
                <option value="" disabled>Select a project</option>
                {remaining.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
            </label>
          )}
          <div className="mappingActions">
            <button type="button" className="button buttonSmall buttonGhost" disabled={saving} onClick={() => setStep(previous ? { projectId: previous.id } : "main")}>
              {previous ? "Previous project" : "Main projects"}
            </button>
            <button type="button" className="button buttonSmall" disabled={disabled} onClick={save}>
              {saving ? "Saving…" : "Save project mapping"}
            </button>
            <button type="button" className="button buttonSmall buttonGhost" disabled={saving} onClick={onClose}>{cancelLabel}</button>
          </div>
        </>
      )}
      {typeof saveStatus === "object" && <p className="pill pill-warn" role="alert">{saveStatus.error}</p>}
    </section>
  );
}
