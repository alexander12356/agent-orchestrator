import { BaseEdge, EdgeLabelRenderer, EdgeProps, getBezierPath } from '@xyflow/react';

export default function ArtifactEdge(props: EdgeProps) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, markerEnd, label, selected, data } = props;
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  const open = (data as { onOpen?: () => void } | undefined)?.onOpen;
  const color = selected ? '#a5b4fc' : '#52617d';
  return <>
    <BaseEdge id={id} path={path} markerEnd={markerEnd} style={{ stroke: color, strokeWidth: selected ? 3 : 1.5 }}/>
    <path d={path} fill="none" stroke="transparent" strokeWidth={28} style={{ pointerEvents: 'stroke', cursor: 'pointer' }} onClick={event => { event.stopPropagation(); open?.(); }}/>
    <EdgeLabelRenderer>
      <button type="button" aria-label={`Открыть ревизию этапа: ${String(label)}`} onMouseDown={event => { event.stopPropagation(); open?.(); }} onClick={event => { event.stopPropagation(); open?.(); }} style={{
        position: 'absolute',
        zIndex: 1000,
        transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
        pointerEvents: 'all',
        cursor: 'pointer',
        border: `1px solid ${selected ? '#a5b4fc' : '#334155'}`,
        borderRadius: 6,
        background: selected ? '#312e81' : '#121c30',
        color: selected ? '#e0e7ff' : '#c3cee2',
        padding: '6px 10px',
        fontSize: 14,
        lineHeight: 1.2,
        whiteSpace: 'nowrap',
      }}>{label}</button>
    </EdgeLabelRenderer>
  </>;
}
