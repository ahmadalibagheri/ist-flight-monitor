/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Emits a minimal server bundle so the runtime image stays small.
  output: "standalone",
  eslint: { ignoreDuringBuilds: false },
};

export default nextConfig;
