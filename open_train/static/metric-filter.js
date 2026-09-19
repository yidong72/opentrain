self.onmessage = ({ data: { keys, query } }) => {
  try {
    const match = query.match(/^\/(.+)\/([imu]*)$/);
    if (!match || query.length > 300)
      throw Error("Use /expression/ or /expression/i (up to 300 characters).");
    const pattern = new RegExp(match[1], match[2]);
    self.postMessage({ keys: keys.filter((key) => pattern.test(key)) });
  } catch (error) {
    self.postMessage({ error: `Invalid metric regex: ${error.message}` });
  }
};
