# bash completion for box-rpg
_box_rpg() {
    local cur prev words cword
    _init_completion || return

    case "${words[1]}" in
        runtime)
            case "${words[2]}" in
                nwjs)
                    case "${words[3]}" in
                        available)
                            COMPREPLY=($(compgen -W '--page --interactive --architecture --sdk --help' -- "$cur"))
                            ;;
                        install|remove)
                            COMPREPLY=($(compgen -W '--architecture --sdk --help' -- "$cur"))
                            ;;
                        list)
                            COMPREPLY=($(compgen -W '--help' -- "$cur"))
                            ;;
                        *)
                            COMPREPLY=($(compgen -W 'list available install remove --help' -- "$cur"))
                            ;;
                    esac
                    ;;
                easyrpg)
                    case "${words[3]}" in
                        list)
                            COMPREPLY=($(compgen -W '--help' -- "$cur"))
                            ;;
                        available)
                            COMPREPLY=($(compgen -W '--page --interactive --help' -- "$cur"))
                            ;;
                        install|remove)
                            COMPREPLY=($(compgen -W '--help' -- "$cur"))
                            ;;
                        *)
                            COMPREPLY=($(compgen -W 'list available install remove --help' -- "$cur"))
                            ;;
                    esac
                    ;;
                *)
                    COMPREPLY=($(compgen -W 'nwjs easyrpg --help' -- "$cur"))
                    ;;
            esac
            ;;
        config)
            case "${words[2]}" in
                show|set)
                    COMPREPLY=($(compgen -W '--help' -- "$cur"))
                    ;;
                *)
                    COMPREPLY=($(compgen -W 'show set --help' -- "$cur"))
                    ;;
            esac
            ;;
        inspect)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W '--help' -- "$cur"))
            else
                COMPREPLY=($(compgen -f -- "$cur"))
            fi
            ;;
        cleanup)
            case "${words[2]}" in
                list)
                    COMPREPLY=($(compgen -W 'roots runtimes downloads profiles --help' -- "$cur"))
                    ;;
                remove)
                    case "${words[3]}" in
                        roots|runtimes|downloads|profiles)
                            if [[ -z "${words[4]}" ]]; then
                                COMPREPLY=($(compgen -W '--all --yes --help' -- "$cur"))
                            else
                                COMPREPLY=($(compgen -W '--yes --help' -- "$cur"))
                            fi
                            ;;
                        *)
                            COMPREPLY=($(compgen -W 'roots runtimes downloads profiles --help' -- "$cur"))
                            ;;
                    esac
                    ;;
                all)
                    COMPREPLY=($(compgen -W '--yes --help' -- "$cur"))
                    ;;
                --interactive)
                    COMPREPLY=($(compgen -W '--help' -- "$cur"))
                    ;;
                --yes)
                    COMPREPLY=($(compgen -W 'all list remove --help' -- "$cur"))
                    ;;
                *)
                    COMPREPLY=($(compgen -W 'all list remove --yes --interactive --help' -- "$cur"))
                    ;;
            esac
            ;;
        launch)
            COMPREPLY=($(compgen -W '--runtime --sdk --copy-root-file --allow-network --allow-game-writes --help' -- "$cur"))
            ;;
        diagnose)
            COMPREPLY=($(compgen -W '--runtime --sdk --help' -- "$cur"))
            ;;
        *)
            COMPREPLY=($(compgen -W 'cleanup inspect runtime launch config diagnose --help --version' -- "$cur"))
            ;;
    esac
}
complete -F _box_rpg box-rpg
