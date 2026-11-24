import { Artifact } from './types';

const stageArtifactPattern: Record<number, RegExp> = {
  1: /^01_tech_doc_v(\d+)\.md$/,
  2: /^02_doc_review_v(\d+)\.json$/,
  3: /^03_impl_report_v(\d+)\.md$/,
  4: /^04_code_review_v(\d+)\.json$/,
};

/** All revisions produced by a stage, ordered from the oldest to the newest. */
export function stageArtifactRevisions(artifacts: Artifact[], stage: number) {
  const pattern = stageArtifactPattern[stage];
  if (!pattern) return [];
  return artifacts.filter(artifact => pattern.test(artifact.name)).sort((a, b) => {
    const revision = (name: string) => Number(name.match(pattern)?.[1] || 0);
    return revision(a.name) - revision(b.name);
  });
}

export function latestStageArtifact(artifacts: Artifact[], stage: number) {
  return stageArtifactRevisions(artifacts, stage).at(-1);
}

export function latestDocumentArtifact(artifacts: Artifact[]) {
  return latestStageArtifact(artifacts, 1);
}
