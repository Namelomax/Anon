/** @type {import('next').NextConfig} */
const nextConfig = {
  // Allow larger uploads to pass through the proxy API route (docx files).
  experimental: {
    serverActions: { bodySizeLimit: "25mb" },
  },
};

export default nextConfig;
