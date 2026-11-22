package diecik.perf.checks;

import diecik.perf.support.Http;
import diecik.perf.support.PerfUser;
import diecik.perf.support.TestEnvironment;
import diecik.perf.support.TokenProvider;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;

/**
 * Live events end to end (GET /api/events through the ALB, between backend tasks). Every chosen client and their
 * dietitian keep a stream open; dietitians send chat messages at a steady rate, and the check measures how long the
 * chat event takes to reach the client's stream (from the start of the send). The streams spread over the tasks
 * behind the ALB, so most deliveries cross from one task to the other.
 *
 * <p>Usage: {@code LiveEventsCheck <conversations> <minutes> <messagesPerMinute>}. Prints the latency percentiles,
 * missing events, stream reconnects and errors; exits with 1 when more than 1% of the events are missing.
 */
public final class LiveEventsCheck {

    private static final Duration DELIVERY_TIMEOUT = Duration.ofSeconds(10);

    private final TestEnvironment env;
    private final TokenProvider tokens;
    /**
     * HTTP/1.1, so every stream has its own connection like a browser tab or a phone. Over HTTP/2 one client would
     * multiplex all streams on one connection, and the ALB refuses more than 128 streams per connection.
     */
    private final HttpClient http = HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofSeconds(10))
            .build();
    /** user -> conversation -> arrival times of chat events (nanoTime). */
    private final Map<String, Map<String, List<Long>>> arrivals = new ConcurrentHashMap<>();
    private final AtomicInteger readyEvents = new AtomicInteger();
    private final AtomicInteger streamErrors = new AtomicInteger();
    private volatile boolean running = true;

    private LiveEventsCheck(TestEnvironment env, List<PerfUser> users) {
        this.env = env;
        this.tokens = new TokenProvider(env, PerfUser.passwords(users));
    }

    public static void main(String[] args) throws Exception {
        TestEnvironment env = TestEnvironment.current().requireNonProd();
        int conversations = Integer.parseInt(args[0]);
        Duration duration = Duration.ofMinutes(Long.parseLong(args[1]));
        double perMinute = Double.parseDouble(args[2]);

        List<PerfUser> all = PerfUser.load(env.usersFile());
        Map<String, PerfUser> dietitianByUnit = all.stream().filter(u -> PerfUser.ROLE_DIETITIAN.equals(u.role()))
                .collect(Collectors.toMap(PerfUser::unit, u -> u));
        List<PerfUser> clients = new ArrayList<>(all.stream().filter(u -> PerfUser.ROLE_CLIENT.equals(u.role())
                && u.trainerClientId() != null).toList());
        Collections.shuffle(clients, new Random(7));
        List<PerfUser> chosen = clients.subList(0, Math.min(conversations, clients.size()));
        Map<String, PerfUser> streamUsers = new LinkedHashMap<>();
        chosen.forEach(client -> {
            streamUsers.put(client.username(), client);
            PerfUser dietitian = dietitianByUnit.get(client.unit());
            streamUsers.put(dietitian.username(), dietitian);
        });

        LiveEventsCheck check = new LiveEventsCheck(env, new ArrayList<>(streamUsers.values()));
        check.run(chosen, dietitianByUnit, streamUsers.values(), duration, perMinute);
    }

    private void run(List<PerfUser> conversations, Map<String, PerfUser> dietitians, Iterable<PerfUser> streamUsers,
                     Duration duration, double perMinute) throws Exception {
        List<PerfUser> users = new ArrayList<>();
        streamUsers.forEach(users::add);
        users.forEach(this::acceptChatPolicy);

        ExecutorService streams = Executors.newVirtualThreadPerTaskExecutor();
        users.forEach(user -> streams.submit(() -> keepStream(user)));
        Instant waitUntil = Instant.now().plusSeconds(60);
        while (readyEvents.get() < users.size() && Instant.now().isBefore(waitUntil)) {
            Thread.sleep(200);
        }
        System.out.printf("%s streams ready: %d/%d%n", Instant.now(), readyEvents.get(), users.size());

        List<Long> latencies = Collections.synchronizedList(new ArrayList<>());
        AtomicInteger missing = new AtomicInteger();
        AtomicInteger sent = new AtomicInteger();
        AtomicInteger sendErrors = new AtomicInteger();
        ExecutorService senders = Executors.newVirtualThreadPerTaskExecutor();
        Random random = new Random(11);
        long interval = (long) (60_000_000_000L / perMinute);
        long end = System.nanoTime() + duration.toNanos();
        long next = System.nanoTime();
        while (System.nanoTime() < end) {
            PerfUser client = conversations.get(random.nextInt(conversations.size()));
            PerfUser dietitian = dietitians.get(client.unit());
            senders.submit(() -> {
                long start = System.nanoTime();
                Http.Response response = Http.send("POST",
                        URI.create(env.apiBase() + "/api/chat/conversations/" + client.trainerClientId() + "/messages"),
                        tokens.accessToken(dietitian.username()),
                        Map.of("clientMessageId", UUID.randomUUID(), "body", "Perf " + Instant.now()));
                if (!response.ok()) {
                    sendErrors.incrementAndGet();
                    return;
                }
                sent.incrementAndGet();
                Long arrival = awaitArrival(client.username(), client.trainerClientId(), start);
                if (arrival == null) {
                    missing.incrementAndGet();
                } else {
                    latencies.add(TimeUnit.NANOSECONDS.toMillis(arrival - start));
                }
            });
            next += interval;
            long sleep = next - System.nanoTime();
            if (sleep > 0) {
                TimeUnit.NANOSECONDS.sleep(sleep);
            }
        }
        senders.shutdown();
        senders.awaitTermination(30, TimeUnit.SECONDS);
        running = false;
        streams.shutdownNow();

        List<Long> sorted = new ArrayList<>(latencies);
        Collections.sort(sorted);
        System.out.printf("Live events: streams=%d conversations=%d sent=%d delivered=%d missing=%d sendErrors=%d"
                        + " readyEvents=%d streamErrors=%d%n", users.size(), conversations.size(), sent.get(),
                sorted.size(), missing.get(), sendErrors.get(), readyEvents.get(), streamErrors.get());
        if (!sorted.isEmpty()) {
            System.out.printf("Latency send start -> event on the client's stream: p50=%d ms p95=%d ms p99=%d ms max=%d ms%n",
                    percentile(sorted, 50), percentile(sorted, 95), percentile(sorted, 99), sorted.getLast());
        }
        System.exit(sent.get() > 0 && missing.get() <= sent.get() / 100 ? 0 : 1);
    }

    private void acceptChatPolicy(PerfUser user) {
        String token = tokens.accessToken(user.username());
        var status = Http.send("GET", URI.create(env.apiBase() + "/api/chat/policy"), token, null)
                .requireOk("Chat policy of " + user.username()).json();
        if (!status.path("accepted").asBoolean(false)) {
            Http.send("PUT", URI.create(env.apiBase() + "/api/chat/policy/acceptance"), token,
                    Map.of("version", status.path("currentVersion").asText()))
                    .requireOk("Accept chat policy for " + user.username());
        }
    }

    /** Keeps one stream open for the user, reconnecting like the apps do. */
    private void keepStream(PerfUser user) {
        while (running) {
            try {
                HttpRequest request = HttpRequest.newBuilder(URI.create(env.apiBase() + "/api/events"))
                        .header("Authorization", "Bearer " + tokens.accessToken(user.username()))
                        .header("Accept", "text/event-stream")
                        .GET().build();
                HttpResponse<java.util.stream.Stream<String>> response =
                        http.send(request, HttpResponse.BodyHandlers.ofLines());
                if (response.statusCode() != 200) {
                    streamErrors.incrementAndGet();
                    Thread.sleep(2_000);
                    continue;
                }
                String event = null;
                for (String line : (Iterable<String>) response.body()::iterator) {
                    if (!running) {
                        return;
                    }
                    if (line.startsWith("event:")) {
                        event = line.substring(6).trim();
                    } else if (line.startsWith("data:") && event != null) {
                        record(user.username(), event, line.substring(5).trim());
                    } else if (line.isEmpty()) {
                        event = null;
                    }
                }
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return;
            } catch (Exception error) {
                if (!running) {
                    return;
                }
                streamErrors.incrementAndGet();
                // Like the apps: wait before reconnecting after a failure.
                try {
                    Thread.sleep(1_000);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }
    }

    private void record(String username, String event, String data) {
        if ("ready".equals(event)) {
            readyEvents.incrementAndGet();
            return;
        }
        if (!"chat".equals(event)) {
            return;
        }
        String clientId = data.replaceAll(".*\"clientId\":\"([^\"]+)\".*", "$1");
        arrivals.computeIfAbsent(username, ignored -> new ConcurrentHashMap<>())
                .computeIfAbsent(clientId, ignored -> Collections.synchronizedList(new ArrayList<>()))
                .add(System.nanoTime());
    }

    /** The first chat event for the conversation that arrived on the user's stream after the send started. */
    private Long awaitArrival(String username, String clientId, long sendStart) {
        long deadline = sendStart + DELIVERY_TIMEOUT.toNanos();
        while (System.nanoTime() < deadline) {
            List<Long> times = arrivals.getOrDefault(username, Map.of()).getOrDefault(clientId, List.of());
            synchronized (times) {
                for (Long time : times) {
                    if (time >= sendStart) {
                        return time;
                    }
                }
            }
            try {
                Thread.sleep(5);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return null;
            }
        }
        return null;
    }

    private static long percentile(List<Long> sorted, int percentile) {
        int index = (int) Math.ceil(percentile / 100.0 * sorted.size()) - 1;
        return sorted.get(Math.max(0, Math.min(index, sorted.size() - 1)));
    }
}
