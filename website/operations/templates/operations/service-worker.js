const GCC_CACHE = "gcc-public-v21";
const GCC_PUBLIC_ASSETS = [
    "/offline/",
    "/static/operations/css/site.css?v=20261028-project-post-workflow-3",
    "/static/operations/js/operations.js?v=20261028-project-post-workflow-3",
    "/static/operations/js/pwa-register.js",
    "/static/operations/images/gcc-logo.png",
    "/static/operations/images/hero-kitchen.png",
    "/static/operations/images/project-adu.png",
    "/static/operations/images/project-bathroom.png",
    "/static/operations/images/progress-kitchen.png"
];
const GCC_PRIVATE_PREFIXES = [
    "/gccad/",
    "/team/",
    "/portal/",
    "/accounts/",
    "/documents/",
    "/media/",
    "/project-covers/"
];

self.addEventListener("install", function (event) {
    event.waitUntil(
        caches.open(GCC_CACHE).then(function (cache) {
            return cache.addAll(GCC_PUBLIC_ASSETS).catch(function () {
                return undefined;
            });
        }).then(function () {
            return self.skipWaiting();
        })
    );
});

self.addEventListener("activate", function (event) {
    event.waitUntil(
        caches.keys().then(function (keys) {
            return Promise.all(keys
                .filter(function (key) {
                    return key.indexOf("gcc-public-") === 0 && key !== GCC_CACHE;
                })
                .map(function (key) {
                    return caches.delete(key);
                }));
        }).then(function () {
            return self.clients.claim();
        })
    );
});

function isPrivatePath(pathname) {
    return GCC_PRIVATE_PREFIXES.some(function (prefix) {
        return pathname === prefix.slice(0, -1) || pathname.indexOf(prefix) === 0;
    });
}

function offlineResponse() {
    return caches.match("/offline/").then(function (response) {
        return response || new Response(
            "Grand Coast Construction is temporarily offline.",
            {status: 503, headers: {"Content-Type": "text/plain; charset=utf-8"}}
        );
    });
}

self.addEventListener("fetch", function (event) {
    const request = event.request;
    if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) {
        return;
    }

    const url = new URL(request.url);
    if (isPrivatePath(url.pathname)) {
        event.respondWith(fetch(request).catch(offlineResponse));
        return;
    }

    if (url.pathname.indexOf("/static/") === 0) {
        event.respondWith(
            caches.match(request).then(function (cached) {
                return cached || fetch(request).then(function (response) {
                    if (response.ok) {
                        const copy = response.clone();
                        caches.open(GCC_CACHE).then(function (cache) {
                            cache.put(request, copy);
                        });
                    }
                    return response;
                });
            })
        );
        return;
    }

    if (request.mode === "navigate") {
        event.respondWith(fetch(request).catch(offlineResponse));
    }
});
