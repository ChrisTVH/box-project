# bash completion for box-rpg
_box_rpg() {
    local cur prev words cword
    _init_completion || return

    case "${words[1]}" in
        runtime)
            case "${words[2]}" in
                available)
                    COMPREPLY=($(compgen -W '--page --interactive --architecture --sdk --help' -- "$cur"))
                    ;;
                install|remove)
                    COMPREPLY=($(compgen -W '--architecture --sdk --help' -- "$cur"))
                    ;;
                easyrpg)
                    case "${words[3]}" in
                        available)
                            COMPREPLY=($(compgen -W '--page --interactive --help' -- "$cur"))
                            ;;
                        *)
                            COMPREPLY=($(compgen -W 'list available install remove --help' -- "$cur"))
                            ;;
                    esac
                    ;;
                *)
                    COMPREPLY=($(compgen -W 'list available install remove easyrpg --help' -- "$cur"))
                    ;;
            esac
            ;;
        config)
            COMPREPLY=($(compgen -W 'show set' -- "$cur"))
            ;;
        launch)
            COMPREPLY=($(compgen -W '--runtime --sdk --help' -- "$cur"))
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
