import { describe, expect, it } from 'vitest';
import { latestDocumentArtifact, latestStageArtifact, stageArtifactRevisions } from './graphUtils';

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

describe('Stage revisions', () => {
  const artifacts = [
    { name: '01_tech_doc_v2.md', path: '/v2', size: 2 },
    { name: '01_tech_doc_v10.md', path: '/v10', size: 10 },
    { name: '02_doc_review_v1.json', path: '/r1', size: 1 },
    { name: '02_doc_review_v9.json', path: '/r9', size: 9 },
    { name: '03_impl_report_v1.md', path: '/i1', size: 1 },
    { name: '04_code_review_v4.json', path: '/c4', size: 4 },
  ];

  it('returns only the revisions of the requested stage in numeric order', () => {
    expect(stageArtifactRevisions(artifacts, 1).map(artifact => artifact.name)).toEqual(['01_tech_doc_v2.md', '01_tech_doc_v10.md']);
    expect(stageArtifactRevisions(artifacts, 2).map(artifact => artifact.name)).toEqual(['02_doc_review_v1.json', '02_doc_review_v9.json']);
  });

  it('selects the newest revision of the stage', () => {
    expect(latestStageArtifact(artifacts, 1)?.name).toBe('01_tech_doc_v10.md');
    expect(latestStageArtifact(artifacts, 2)?.name).toBe('02_doc_review_v9.json');
  });

  it('returns no revisions for an unknown stage', () => {
    expect(stageArtifactRevisions(artifacts, 5)).toEqual([]);
  });
});
