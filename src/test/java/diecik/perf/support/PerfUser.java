package diecik.perf.support;

import com.fasterxml.jackson.core.type.TypeReference;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * A synthetic test account. {@code canary} is a marker unique to this user that is planted in their data and must
 * never show up in a response to anyone else (data isolation check I-1).
 */
public record PerfUser(
        String username,
        String email,
        String password,
        String role,
        String unit,
        String canary,
        String keycloakId
) {

    public static final String ROLE_INDIVIDUAL = "individual";
    public static final String ROLE_DIETITIAN = "dietitian";
    public static final String ROLE_CLIENT = "client";

    public static List<PerfUser> load(Path file) {
        if (!Files.exists(file)) {
            throw new IllegalStateException("No seeded users in " + file + ". Run SeedMain first (see README).");
        }
        try {
            return Http.JSON.readValue(file.toFile(), new TypeReference<List<PerfUser>>() { });
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    public static List<PerfUser> load(Path file, String role) {
        List<PerfUser> users = load(file).stream().filter(user -> role.equals(user.role())).toList();
        if (users.isEmpty()) {
            throw new IllegalStateException("No seeded users with role '" + role + "' in " + file);
        }
        return users;
    }

    /** Merges by username, so re-running a seed step keeps the existing accounts. */
    public static void save(Path file, List<PerfUser> users) {
        try {
            Files.createDirectories(file.getParent());
            List<PerfUser> merged = new ArrayList<>(Files.exists(file) ? load(file) : List.of());
            Map<String, PerfUser> byName = merged.stream()
                    .collect(Collectors.toMap(PerfUser::username, Function.identity(), (a, b) -> b,
                            java.util.LinkedHashMap::new));
            users.forEach(user -> byName.put(user.username(), user));
            Http.JSON.writerWithDefaultPrettyPrinter().writeValue(file.toFile(), new ArrayList<>(byName.values()));
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    public static Map<String, String> passwords(List<PerfUser> users) {
        return users.stream().collect(Collectors.toMap(PerfUser::username, PerfUser::password));
    }
}
