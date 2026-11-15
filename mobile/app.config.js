const appJson = require('./app.json');

module.exports = ({ config }) => {
  const projectId = process.env.EXPO_PUBLIC_EXPO_PROJECT_ID
    || config.extra?.eas?.projectId
    || appJson.expo.extra?.eas?.projectId
    || (config.extra?.expoProjectId && !config.extra.expoProjectId.startsWith('REPLACE_') ? config.extra.expoProjectId : undefined)
    || (appJson.expo.extra?.expoProjectId && !appJson.expo.extra.expoProjectId.startsWith('REPLACE_') ? appJson.expo.extra.expoProjectId : undefined);
  return {
    ...appJson.expo,
    ...config,
    extra: {
      ...(appJson.expo.extra || {}),
      ...(config.extra || {}),
      expoProjectId: projectId || 'REPLACE_WITH_EXPO_PROJECT_ID',
    },
  };
};
