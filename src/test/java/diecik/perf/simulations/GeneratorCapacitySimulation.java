package diecik.perf.simulations;

import diecik.perf.support.PerfUser;
import diecik.perf.support.TestEnvironment;
import diecik.perf.support.TokenProvider;
import io.gatling.javaapi.core.ChainBuilder;
import io.gatling.javaapi.core.ClosedInjectionStep;
import io.gatling.javaapi.core.FeederBuilder;
import io.gatling.javaapi.core.ScenarioBuilder;
import io.gatling.javaapi.core.Simulation;
import io.gatling.javaapi.http.HttpProtocolBuilder;

import java.time.Duration;
import java.time.LocalDate;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

import static io.gatling.javaapi.core.CoreDsl.StringBody;
import static io.gatling.javaapi.core.CoreDsl.constantConcurrentUsers;
import static io.gatling.javaapi.core.CoreDsl.constantUsersPerSec;
import static io.gatling.javaapi.core.CoreDsl.exec;
import static io.gatling.javaapi.core.CoreDsl.listFeeder;
import static io.gatling.javaapi.core.CoreDsl.pause;
import static io.gatling.javaapi.core.CoreDsl.scenario;
import static io.gatling.javaapi.http.HttpDsl.header;
import static io.gatling.javaapi.http.HttpDsl.http;
import static io.gatling.javaapi.http.HttpDsl.status;

/**
 * W-02 generator capacity curve and W-03 noisy neighbour in one run.
 *
 * <p>The generator scenario keeps a fixed number of week previews in flight (closed model) and steps that number up
 * ({@code -Dperf.steps=1,2,4,8}). Hypothesis H1: with one effective CPU the generator runs on a single
 * {@code Schedulers.parallel()} thread, so latency grows roughly linearly with concurrency while throughput stays flat.
 *
 * <p>Two light probes run at a constant rate during the whole test: {@code /public/health} (event loop only, no DB)
 * and {@code GET /api/diet-day} (one DB read). If generator load pushes their latency up, the generator slows down
 * the rest of the API (H2 GC pauses, H3 CPU throttling, H4 timer starvation).
 *
 * <p>Requests are named after the concurrency step they ran in ({@code c=4}), so the console and the report give
 * latency per step. Inputs: {@code -Dperf.inputs=varied} (default) gives every user a deterministic profile of their
 * own (calories, meals per day, allergies), like real clients of a dietitian; {@code fixed} sends one identical
 * request, as the backend's own generator benchmark does, for before/after comparisons.
 */
public class GeneratorCapacitySimulation extends Simulation {

    private final TestEnvironment env = TestEnvironment.current().requireNonProd();
    private final List<PerfUser> users = PerfUser.load(env.usersFile(), PerfUser.ROLE_INDIVIDUAL);
    private final TokenProvider tokens = new TokenProvider(env, PerfUser.passwords(users));

    private final int[] steps = Arrays.stream(System.getProperty("perf.steps", "1,2,4,8").split(","))
            .map(String::trim).mapToInt(Integer::parseInt).toArray();
    private final Duration stepDuration = Duration.ofSeconds(Integer.getInteger("perf.stepSeconds", 90));
    private final int days = Integer.getInteger("perf.days", 7);
    private final String dietType = System.getProperty("perf.dietType", "standard");
    private final double probeRate = Double.parseDouble(System.getProperty("perf.probeRate", "2"));
    private final boolean variedInputs = !"fixed".equals(System.getProperty("perf.inputs", "varied"));
    /** true (default): a user who got 429 waits Retry-After, as the apps do; false: retry at once (abuse). */
    private final boolean respectRetryAfter = Boolean.parseBoolean(System.getProperty("perf.respectRetryAfter", "true"));
    private volatile long startNanos = System.nanoTime();

    private final FeederBuilder<Object> userFeeder = listFeeder(java.util.stream.IntStream.range(0, users.size())
            .mapToObj(index -> Map.<String, Object>of("username", users.get(index).username(), "profile", index))
            .toList()).circular();

    private final ChainBuilder authorize = exec(session ->
            session.set("token", tokens.accessToken(session.getString("username"))));

    private final ScenarioBuilder generator = scenario("generator")
            .feed(userFeeder)
            .exec(authorize)
            .exec(http(session -> "diet preview " + days + "d c=" + currentStep())
                    .post("/api/diet/generate/preview")
                    .header("Authorization", "Bearer #{token}")
                    .body(StringBody(session -> previewBody(session.getInt("profile"))))
                    .asJson()
                    .check(status().saveAs("status"),
                            header("Retry-After").optional().saveAs("retryAfter"),
                            status().is(200)))
            // Like the apps: after 429 wait as long as Retry-After says before this user tries again.
            .doIf(session -> "429".equals(String.valueOf(session.get("status"))) && respectRetryAfter)
            .then(pause(session -> Duration.ofSeconds(Long.parseLong(
                    String.valueOf(session.contains("retryAfter") ? session.get("retryAfter") : "10")))));

    private final ScenarioBuilder healthProbe = scenario("probe health")
            .exec(http(session -> "probe /public/health c=" + currentStep())
                    .get("/public/health")
                    .header("Accept", "text/plain")
                    .check(status().is(200)));

    private final ScenarioBuilder readProbe = scenario("probe read")
            .feed(userFeeder)
            .exec(authorize)
            .exec(http(session -> "probe GET /api/diet-day c=" + currentStep())
                    .get("/api/diet-day")
                    .queryParam("date", session -> LocalDate.now().toString())
                    .header("Authorization", "Bearer #{token}")
                    .check(status().in(200, 204, 404)));

    {
        Duration total = stepDuration.multipliedBy(steps.length);
        ClosedInjectionStep[] injection = Arrays.stream(steps)
                .mapToObj(concurrency -> constantConcurrentUsers(concurrency).during(stepDuration))
                .toArray(ClosedInjectionStep[]::new);

        HttpProtocolBuilder protocol = http.baseUrl(env.apiBase().toString())
                .acceptHeader("application/json")
                .userAgentHeader("diecik-perf/" + env.name());

        setUp(
                generator.injectClosed(injection),
                healthProbe.injectOpen(constantUsersPerSec(probeRate).during(total)),
                readProbe.injectOpen(constantUsersPerSec(probeRate).during(total))
        ).protocols(protocol);
    }

    @Override
    public void before() {
        // Log every test user in before the clock starts, so Keycloak is not part of the measurement.
        users.forEach(user -> tokens.accessToken(user.username()));
        System.out.printf("Generator capacity: env=%s steps=%s step=%ss days=%d dietType=%s inputs=%s users=%d%n",
                env.name(), Arrays.toString(steps), stepDuration.toSeconds(), days, dietType,
                variedInputs ? "varied" : "fixed", users.size());
        startNanos = System.nanoTime();
    }

    private int currentStep() {
        long index = (System.nanoTime() - startNanos) / stepDuration.toNanos();
        return steps[(int) Math.min(Math.max(index, 0), steps.length - 1)];
    }

    /** Deterministic per-user profile: 1500–3000 kcal, 3–6 meals, some users with allergies. */
    private String previewBody(int profile) {
        int calories = variedInputs ? 1500 + (profile * 137) % 1501 : 2200;
        int meals = variedInputs ? 3 + profile % 4 : 5;
        List<String> allergies = new java.util.ArrayList<>();
        if (variedInputs && profile % 5 == 0) {
            allergies.add("\"gluten\"");
        }
        if (variedInputs && profile % 7 == 0) {
            allergies.add("\"mleko\"");
        }
        if (variedInputs && profile % 11 == 0) {
            allergies.add("\"jaja\"");
            allergies.add("\"ryby\"");
        }
        return """
                {"startDate":"%s","dietType":"%s","mealsPerDay":%d,"days":%d,"allergies":[%s],"tastePreferences":[],
                 "macros":{"calories":%d,"protein":%d,"carbs":%d,"fat":%d,"fiber":30}}
                """.formatted(LocalDate.now().plusDays(1), dietType, meals, days, String.join(",", allergies),
                calories, calories / 16, calories / 8, calories / 36);
    }
}
