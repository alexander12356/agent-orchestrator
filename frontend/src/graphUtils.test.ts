import { describe, expect, it } from 'vitest';
import { edgeArtifact, latestDocumentArtifact } from './graphUtils';
import { StageRun } from './types';

function run(stage: number, attempt: number, artifact: string, status = 'completed'): StageRun {
  return {
    id: attempt,
    task_id: 1,
    stage,
    attempt,
    is_fallback: false,
    artifact_path: `/storage/tasks/1/${artifact}`,
    input_tokens: 0,
    output_tokens: 0,
    cache_tokens: 0,
    tokens_estimated: false,
    status,
    started_at: '2026-01-01T00:00:00Z',
  };
}

describe('Graph edge artifacts', () => {
  it('opens the latest documentation on a forward 1 → 2 edge', () => {
    const runs = [run(1, 1, '01_tech_doc_v1.md'), run(1, 2, '01_tech_doc_v2.md')];
    expect(edgeArtifact(runs, 1, 2)).toBe('01_tech_doc_v2.md');
  });

  it('opens the latest successful review on a return 2 → 1 edge', () => {
    const runs = [
      run(2, 1, '02_doc_review_v1.json'),
      run(2, 2, '02_doc_review_v2.json'),
      run(2, 3, '02_doc_review_v3.json', 'error'),
    ];
    expect(edgeArtifact(runs, 2, 1)).toBe('02_doc_review_v2.json');
  });
});


describe('Latest documentation', () => {
  it('selects the highest numeric revision', () => {
    const artifacts = [
      { name: '01_tech_doc_v2.md', path: '/v2', size: 2 },
      { name: '02_doc_review_v11.json', path: '/review', size: 0 },
      { name: '01_tech_doc_v10.md', path: '/v10', size: 10 },
    ];
    expect(latestDocumentArtifact(artifacts)?.name).toBe('01_tech_doc_v10.md');
  });
});
