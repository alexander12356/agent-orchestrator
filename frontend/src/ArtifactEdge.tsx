import { BaseEdge, EdgeLabelRenderer, EdgeProps, getSmoothStepPath } from '@xyflow/react';

export default function ArtifactEdge(props: EdgeProps) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, markerEnd, label, selected, style, data } = props;
  const [path] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 12 });
  const labelY = (sourceY + targetY) / 2;
  const open = (data as { onOpen?: () => void } | undefined)?.onOpen;
  return <>
    <BaseEdge id={id} path={path} markerEnd={markerEnd} interactionWidth={36} style={{ ...style, stroke: selected ? '#a5b4fc' : style?.stroke, strokeWidth: selected ? 3 : 1.5 }}/>
    <EdgeLabelRenderer>
      <button type="button" className="nodrag nopan" title={String(label)} aria-label={`Открыть ревизию этапа: ${String(label)}`} onClick={event => { event.stopPropagation(); open?.(); }} style={{
        position: 'absolute',
        zIndex: 1000,
        transform: `translate(-50%, -50%) translate(${targetX}px, ${labelY}px)`,
        pointerEvents: 'all',
        cursor: 'pointer',
        border: `1px solid ${selected ? '#a5b4fc' : '#334155'}`,
        borderRadius: 6,
        background: selected ? '#312e81' : '#121c30',
        color: selected ? '#e0e7ff' : '#c3cee2',
        padding: '6px 10px',
        fontSize: 14,
        lineHeight: 1.2,
        maxWidth: 250,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      }}>{label}</button>
    </EdgeLabelRenderer>
  </>;
}
