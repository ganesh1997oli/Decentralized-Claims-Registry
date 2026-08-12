// Vite builds the browser application; Vitest reuses the same module pipeline
// while limiting discovery to the established colocated tests and the dedicated
// app-level test folder. New feature tests can stay outside production `src`, in
// the same style as the sibling `apps/contracts/test` directory.
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    include: ['src/**/*.test.{ts,tsx}', 'test/**/*.test.{ts,tsx}'],
  },
})
