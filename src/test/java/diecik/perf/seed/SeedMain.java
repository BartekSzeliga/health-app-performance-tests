package diecik.perf.seed;

import diecik.perf.support.Http;
import diecik.perf.support.KeycloakAdmin;
import diecik.perf.support.PerfUser;
import diecik.perf.support.TestEnvironment;
import diecik.perf.support.TokenProvider;

import java.net.URI;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HexFormat;
import java.util.List;

/**
 * Prepares a test environment. Commands:
 * <ul>
 *   <li>{@code setup-realm}: enable the password grant on the app client (test realm only)</li>
 *   <li>{@code individuals <count>}: individual app users for generator measurements (W-02, W-03)</li>
 *   <li>{@code dietitians <count> <clientsPerDietitian> [threads]}: dietitian units for W-14 and W-15
 *       (PRO plan, data processing agreement, linked clients with a published plan)</li>
 * </ul>
 * Accounts land in {@code results/<env>/users.json} (git-ignored) and use the reserved domain
 * {@code perf.diecik.invalid}, so no e-mail can ever be delivered.
 */
public final class SeedMain {

    private static final SecureRandom RANDOM = new SecureRandom();

    private SeedMain() {
    }

    public static void main(String[] args) {
        TestEnvironment env = TestEnvironment.current().requireNonProd();
        String command = args.length == 0 ? "" : args[0];
        switch (command) {
            case "setup-realm" -> {
                new KeycloakAdmin(env).enableDirectAccessGrants(env.clientId());
                System.out.println("Password grant enabled for client " + env.clientId() + " in " + env.name());
            }
            case "individuals" -> seedIndividuals(env, Integer.parseInt(args[1]));
            case "dietitians" -> new DietitianUnitSeeder(env).seed(Integer.parseInt(args[1]), Integer.parseInt(args[2]),
                    args.length > 3 ? Integer.parseInt(args[3]) : 4);
            default -> {
                System.err.println("Usage: SeedMain setup-realm | individuals <count>"
                        + " | dietitians <count> <clientsPerDietitian> [threads]");
                System.exit(2);
            }
        }
    }

    static void seedIndividuals(TestEnvironment env, int count) {
        KeycloakAdmin admin = new KeycloakAdmin(env);
        List<PerfUser> users = new ArrayList<>();
        for (int i = 1; i <= count; i++) {
            String username = "perf-ind-%04d".formatted(i);
            String email = username + "@perf.diecik.invalid";
            String password = randomPassword();
            String id = admin.ensureUser(username, email, "Perf", "Individual %04d".formatted(i), password);
            users.add(new PerfUser(username, email, password, PerfUser.ROLE_INDIVIDUAL, null,
                    username + "-" + HexFormat.of().formatHex(randomBytes(4)), id, null, null));
        }
        PerfUser.save(env.usersFile(), users);

        // The first authenticated call provisions the backend user record, outside the measured run.
        TokenProvider tokens = new TokenProvider(env, PerfUser.passwords(users));
        int provisioned = 0;
        for (PerfUser user : users) {
            Http.Response response = Http.send("GET", URI.create(env.apiBase() + "/api/user-details"),
                    tokens.accessToken(user.username()), null);
            if (response.status() == 401 || response.status() >= 500) {
                throw new IllegalStateException("Backend rejected " + user.username() + ": HTTP " + response.status()
                        + " " + response.body());
            }
            provisioned++;
        }
        System.out.printf("Seeded %d individual users into %s (%d provisioned in backend)%n",
                users.size(), env.usersFile(), provisioned);
    }

    static String randomPassword() {
        return "Perf-" + Base64.getUrlEncoder().withoutPadding().encodeToString(randomBytes(15)) + "9a";
    }

    private static byte[] randomBytes(int length) {
        byte[] bytes = new byte[length];
        RANDOM.nextBytes(bytes);
        return bytes;
    }
}
