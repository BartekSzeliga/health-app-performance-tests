package diecik.perf.simulations;

import diecik.perf.support.PerfUser;
import diecik.perf.support.TestEnvironment;
import diecik.perf.support.TokenProvider;
import io.gatling.javaapi.core.ChainBuilder;
import io.gatling.javaapi.core.FeederBuilder;
import io.gatling.javaapi.core.OpenInjectionStep;
import io.gatling.javaapi.core.PopulationBuilder;
import io.gatling.javaapi.core.ScenarioBuilder;
import io.gatling.javaapi.core.Simulation;
import io.gatling.javaapi.http.HttpProtocolBuilder;
import io.gatling.javaapi.http.HttpRequestActionBuilder;

import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.stream.Collectors;

import static io.gatling.javaapi.core.CoreDsl.StringBody;
import static io.gatling.javaapi.core.CoreDsl.constantUsersPerSec;
import static io.gatling.javaapi.core.CoreDsl.exec;
import static io.gatling.javaapi.core.CoreDsl.listFeeder;
import static io.gatling.javaapi.core.CoreDsl.scenario;
import static io.gatling.javaapi.http.HttpDsl.http;
import static io.gatling.javaapi.http.HttpDsl.status;

/**
 * W-14: the busy-hour traffic of N dietitian units (1 dietitian + 60 clients each), stepped up to find how many units
 * a configuration carries ({@code -Dperf.units=25,50,100,...}, {@code -Dperf.stepSeconds=180}).
 *
 * <p>Open model: every action arrives at a fixed rate per unit, independent of how fast the server answers, like real
 * users. Rates per unit and second (busy hour, 42 of 60 clients active, see PLAN.md §7.1 and §10.4):
 * <ul>
 *   <li>client opens the app (8 parallel GETs of the start screen): 0.0070</li>
 *   <li>client views the plan and the shopping list: 0.00175</li>
 *   <li>client logs water: 0.00875</li>
 *   <li>client chat open, one poll of 2 GETs: 0.021</li>
 *   <li>dietitian chat open (panel polls every 5 s, 2 GETs): 0.05</li>
 *   <li>dietitian opens a client card (5 GETs): 0.00083</li>
 *   <li>dietitian generates a 7-day draft (generator, gated): 0.0011</li>
 * </ul>
 * About 0.23 requests per second per unit. {@code -Dperf.trafficFactor} scales all rates, {@code -Dperf.generator=false}
 * drops the drafts. Virtual units reuse the seeded accounts in turn; request names carry the step ({@code u=100}).
 */
public class DietitianUnitsSimulation extends Simulation {

    private static final double APP_OPEN = 0.0070;
    private static final double PLAN_VIEW = 0.00175;
    private static final double WATER = 0.00875;
    private static final double CLIENT_CHAT_POLL = 0.021;
    private static final double DIETITIAN_CHAT_POLL = 0.05;
    private static final double CARD_VIEW = 0.00083;
    private static final double DRAFT = 0.0011;

    private final TestEnvironment env = TestEnvironment.current().requireNonProd();
    private final List<PerfUser> clients = PerfUser.load(env.usersFile(), PerfUser.ROLE_CLIENT).stream()
            .filter(client -> client.trainerClientId() != null && client.planId() != null).toList();
    private final List<PerfUser> dietitians = PerfUser.load(env.usersFile(), PerfUser.ROLE_DIETITIAN);
    private final TokenProvider tokens = new TokenProvider(env, PerfUser.passwords(
            java.util.stream.Stream.concat(clients.stream(), dietitians.stream()).toList()));

    private final int[] steps = Arrays.stream(System.getProperty("perf.units", "25,50,100").split(","))
            .map(String::trim).mapToInt(Integer::parseInt).toArray();
    private final Duration stepDuration = Duration.ofSeconds(Integer.getInteger("perf.stepSeconds", 180));
    private final double trafficFactor = Double.parseDouble(System.getProperty("perf.trafficFactor", "1"));
    private final boolean generator = Boolean.parseBoolean(System.getProperty("perf.generator", "true"));
    /** false: the apps get live events (GET /api/events) and do not poll the chat; streams come from LiveEventsCheck. */
    private final boolean chatPolling = Boolean.parseBoolean(System.getProperty("perf.chatPolling", "true"));
    private volatile long startNanos = System.nanoTime();

    private final FeederBuilder<Object> clientFeeder = listFeeder(clients.stream()
            .map(client -> Map.<String, Object>of(
                    "username", client.username(), "cardId", client.trainerClientId(), "planId", client.planId()))
            .toList()).random();

    /** A dietitian with one of their own cards: the panel always works on the dietitian's own clients. */
    private final FeederBuilder<Object> dietitianFeeder;

    {
        Map<String, List<String>> cardsByUnit = clients.stream().collect(Collectors.groupingBy(PerfUser::unit,
                Collectors.mapping(PerfUser::trainerClientId, Collectors.toList())));
        List<Map<String, Object>> records = new ArrayList<>();
        for (PerfUser dietitian : dietitians) {
            for (String card : cardsByUnit.getOrDefault(dietitian.unit(), List.of())) {
                records.add(Map.of("username", dietitian.username(), "cardId", card));
            }
        }
        dietitianFeeder = listFeeder(records).random();
    }

    private final ChainBuilder authorize = exec(session -> session
            .set("token", tokens.accessToken(session.getString("username")))
            .set("today", LocalDate.now().toString()));

    private HttpRequestActionBuilder get(String name, String path) {
        return http(session -> name + " u=" + currentStep())
                .get(path)
                .header("Authorization", "Bearer #{token}")
                .check(status().is(200));
    }

    private final ScenarioBuilder appOpen = scenario("client app open")
            .feed(clientFeeder).exec(authorize)
            .exec(get("app open: user details", "/api/users/user/details").resources(
                    get("app open: nav status", "/api/trainer/nav-status"),
                    get("app open: chat list", "/api/chat/conversations?perspective=CLIENT"),
                    get("app open: questionnaires", "/api/client/questionnaires"),
                    get("app open: plan summaries", "/api/trainer/shared-plans/v2/summaries?limit=20"),
                    get("app open: training day", "/api/training-activities/day?date=#{today}"),
                    get("app open: subscription", "/api/subscriptions/me"),
                    get("app open: water", "/api/water-intake?date=#{today}")));

    private final ScenarioBuilder planView = scenario("client plan view")
            .feed(clientFeeder).exec(authorize)
            .exec(get("plan: details", "/api/trainer/shared-plans/#{planId}"))
            .exec(get("plan: shopping list", "/api/trainer/shared-plans/#{planId}/shopping-list"));

    private final ScenarioBuilder water = scenario("client water")
            .feed(clientFeeder).exec(authorize)
            .exec(http(session -> "water: add u=" + currentStep())
                    .put(session -> "/api/water-intake/" + UUID.randomUUID())
                    .header("Authorization", "Bearer #{token}")
                    .body(StringBody("{\"date\":\"#{today}\",\"amountMl\":250}")).asJson()
                    .check(status().is(200)));

    private final ScenarioBuilder clientChat = scenario("client chat poll")
            .feed(clientFeeder).exec(authorize)
            .exec(get("client chat: list", "/api/chat/conversations?perspective=CLIENT"))
            .exec(get("client chat: messages", "/api/chat/conversations/#{cardId}/messages?limit=30"));

    private final ScenarioBuilder dietitianChat = scenario("dietitian chat poll")
            .feed(dietitianFeeder).exec(authorize)
            .exec(get("panel chat: conversation", "/api/chat/conversations?perspective=TRAINER&clientId=#{cardId}"))
            .exec(get("panel chat: messages", "/api/chat/conversations/#{cardId}/messages?limit=30"));

    private final ScenarioBuilder cardView = scenario("dietitian card view")
            .feed(dietitianFeeder).exec(authorize)
            .exec(get("card: client", "/api/trainer/clients/#{cardId}").resources(
                    // 403 until the client shares health data with the dietitian; the panel makes the same call.
                    http(session -> "card: health data u=" + currentStep())
                            .get("/api/trainer/clients/#{cardId}/health-data")
                            .header("Authorization", "Bearer #{token}")
                            .check(status().in(200, 403)),
                    get("card: follow-ups", "/api/trainer/clients/#{cardId}/follow-ups"),
                    get("card: snapshots", "/api/trainer/clients/#{cardId}/snapshots"),
                    get("card: questionnaires", "/api/trainer/clients/#{cardId}/questionnaires")));

    private final ScenarioBuilder draft = scenario("dietitian draft")
            .feed(dietitianFeeder).exec(authorize)
            .exec(http(session -> "draft: generate 7d u=" + currentStep())
                    .post("/api/trainer/clients/#{cardId}/diet-snapshot/generate-draft-v2")
                    .header("Authorization", "Bearer #{token}")
                    .body(StringBody(session -> draftBody())).asJson()
                    .check(status().is(200)));

    {
        HttpProtocolBuilder protocol = http.baseUrl(env.apiBase().toString())
                .acceptHeader("application/json")
                .userAgentHeader("diecik-perf/" + env.name())
                .shareConnections();

        List<PopulationBuilder> populations = new ArrayList<>(List.of(
                inject(appOpen, APP_OPEN),
                inject(planView, PLAN_VIEW),
                inject(water, WATER),
                inject(cardView, CARD_VIEW)));
        if (chatPolling) {
            populations.add(inject(clientChat, CLIENT_CHAT_POLL));
            populations.add(inject(dietitianChat, DIETITIAN_CHAT_POLL));
        }
        if (generator) {
            populations.add(inject(draft, DRAFT));
        }
        setUp(populations).protocols(protocol);
    }

    private PopulationBuilder inject(ScenarioBuilder scenario, double ratePerUnit) {
        OpenInjectionStep[] injection = Arrays.stream(steps)
                .mapToObj(units -> constantUsersPerSec(ratePerUnit * units * trafficFactor)
                        .during(stepDuration).randomized())
                .toArray(OpenInjectionStep[]::new);
        return scenario.injectOpen(injection);
    }

    @Override
    public void before() {
        // Log every account in before the clock starts, so Keycloak logins are not part of the measurement.
        clients.forEach(client -> tokens.accessToken(client.username()));
        dietitians.forEach(dietitian -> tokens.accessToken(dietitian.username()));
        startNanos = System.nanoTime();
        Instant start = Instant.now();
        System.out.printf("Dietitian units: env=%s steps=%s step=%ss factor=%s generator=%s chatPolling=%s seeded=%d"
                        + " dietitians, %d clients%n", env.name(), Arrays.toString(steps), stepDuration.toSeconds(),
                trafficFactor, generator, chatPolling, dietitians.size(), clients.size());
        for (int index = 0; index < steps.length; index++) {
            Instant from = start.plus(stepDuration.multipliedBy(index));
            System.out.printf("STEP u=%d from=%s to=%s%n", steps[index], from, from.plus(stepDuration));
        }
    }

    private int currentStep() {
        long index = (System.nanoTime() - startNanos) / stepDuration.toNanos();
        return steps[(int) Math.min(Math.max(index, 0), steps.length - 1)];
    }

    private String draftBody() {
        LocalDate start = LocalDate.now().plusDays(8);
        String dates = java.util.stream.IntStream.range(0, 7)
                .mapToObj(day -> "\"" + start.plusDays(day) + "\"").collect(Collectors.joining(","));
        int calories = 1600 + (int) (Math.random() * 1200);
        return """
                {"periodStart":"%s","periodEnd":"%s","targetDates":[%s],"dietType":"standard","mealsPerDay":5,
                 "kcal":%d,"protein":%d,"carbs":%d,"fat":%d,"fiber":30,"allergies":[],"tastePreferences":[]}
                """.formatted(start, start.plusDays(6), dates, calories, calories / 16, calories / 8, calories / 36);
    }
}
