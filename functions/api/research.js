export async function onRequest(context) {
  return context.env.RESEARCH.fetch(context.request);
}
