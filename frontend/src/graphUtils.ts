import { Artifact, StageRun } from './types';

export function edgeArtifact(runs: StageRun[], source: number, target: number) {
  if (source === target) return undefined;
  const run = runs.filter(item => item.stage === source && item.status === 'completed' && item.artifact_path).at(-1);
  return run?.artifact_path?.split(/[\\/]/).at(-1);
}


export function latestDocumentArtifact(artifacts: Artifact[]) {
  return artifacts.filter(artifact => /^01_tech_doc_v\d+\.md$/.test(artifact.name)).sort((a, b) => {
    const revision = (name: string) => Number(name.match(/_v(\d+)\.md$/)?.[1] || 0);
    return revision(a.name) - revision(b.name);
  }).at(-1);
}
