import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 3000,
    proxy: {
      '/api/v1/agents': process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011',
      '/api/v1/agent-conversations': process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011',
      '/api/v1/agent-runs': process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011',
      '/api/v1/tools': process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011',
      '/api/v1/evaluations': process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011',
      '/api/v1/local-agent': process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011',
      '/api/v1/tasks': process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011',
      '/api/v1/task-dispatch-events': process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011',
      '/api/v1/notifications': process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011',
      // Keep the browser on a same-origin relative API while allowing the
      // development server to run on a non-conflicting local port.
      '/api': process.env.CHAT_SERVER_URL || 'http://127.0.0.1:9010',
      '/ws': {
        target: (process.env.CHAT_SERVER_URL || 'http://127.0.0.1:9010').replace(/^http/, 'ws'),
        ws: true,
      },
      '/agent/ws': {
        target: (process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011').replace(/^http/, 'ws'),
        ws: true,
      },
      '/task/ws': {
        target: (process.env.AGENT_SERVER_URL || 'http://127.0.0.1:9011').replace(/^http/, 'ws'),
        ws: true,
      },
    },
  },
});
