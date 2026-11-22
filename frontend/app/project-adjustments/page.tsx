"use client";

import { PageHeader } from "../components/PageHeader";
import { ProjectAdjustmentsBoard } from "../components/ProjectAdjustmentsBoard";

export default function ProjectAdjustmentsPage() {
  return (
    <>
      <PageHeader
        eyebrow="Operations"
        title="Project Adjustments"
        lede="Choose main projects and group active or archived projects under them."
      />
      <ProjectAdjustmentsBoard />
    </>
  );
}
