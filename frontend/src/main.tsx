import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import ArtifactPage from './ArtifactPage';
import './index.css';
import '@xyflow/react/dist/style.css';

const params = new URLSearchParams(window.location.search);
const taskId = Number(params.get('task'));
const artifact = params.get('name');
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {window.location.pathname === '/artifact' && Number.isSafeInteger(taskId) && taskId > 0 && artifact
      ? <ArtifactPage taskId={taskId} name={artifact}/>
      : <App/>}
  </React.StrictMode>,
);
