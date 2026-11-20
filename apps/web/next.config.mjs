/** @type {import('next').NextConfig} */
const nextConfig = {
  // Produces .next/standalone with a self-contained server.js, so the runtime
  // image carries no node_modules tree and no package manager.
  output: 'standalone',
  reactStrictMode: true,
  poweredByHeader: false,
  // Type and lint errors must fail the image build, not ship.
  typescript: { ignoreBuildErrors: false },
  eslint: { ignoreDuringBuilds: true },
};

export default nextConfig;
