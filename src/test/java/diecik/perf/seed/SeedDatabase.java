package diecik.perf.seed;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.util.HexFormat;
import java.util.UUID;

/**
 * The one seeding step that cannot go through the API: in test environments mail is off, so the verification e-mail
 * with the invitation token never arrives. This replaces only the delivery: the token row of a test client gets the
 * hash of a token the seeder knows. Accepting and linking then go through the API.
 *
 * <p>Connection: {@code -Dperf.dbUrl}, {@code -Dperf.dbUser}, {@code -Dperf.dbPassword} (or {@code PERF_DB_*}).
 * Local defaults point at the perf stack on port 15432. Only rows of {@code @perf.diecik.invalid} clients are touched.
 */
final class SeedDatabase {

    private final String url = setting("dbUrl", "jdbc:postgresql://localhost:15432/dietdb");
    private final String user = setting("dbUser", "postgres");
    private final String password = setting("dbPassword", "postgres");

    void deliverVerificationToken(UUID clientCardId, String rawToken) {
        String sql = """
                UPDATE trainer_client_email_tokens t SET token_hash = ?
                FROM trainer_clients c
                WHERE t.client_id = c.id AND c.id = ? AND c.email LIKE '%@perf.diecik.invalid'
                  AND t.purpose = 'VERIFY_CLIENT_EMAIL' AND t.accepted_at IS NULL AND t.declined_at IS NULL
                """;
        try (Connection connection = DriverManager.getConnection(url, user, password);
             PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, sha256Hex(rawToken));
            statement.setObject(2, clientCardId);
            if (statement.executeUpdate() != 1) {
                throw new IllegalStateException("No open verification token for test client card " + clientCardId);
            }
        } catch (SQLException e) {
            throw new IllegalStateException("Could not deliver the verification token for " + clientCardId, e);
        }
    }

    /** Same as the backend's SecureTokens.sha256Hex. */
    private static String sha256Hex(String rawToken) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(digest.digest(rawToken.trim().getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String setting(String key, String fallback) {
        String property = System.getProperty("perf." + key);
        if (property != null && !property.isBlank()) {
            return property;
        }
        String variable = System.getenv("PERF_" + key.replaceAll("([a-z])([A-Z])", "$1_$2").toUpperCase());
        return variable != null && !variable.isBlank() ? variable : fallback;
    }
}
